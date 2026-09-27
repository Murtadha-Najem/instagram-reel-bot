"""Stage 2: find the moments worth looking at, group them into shots, read on-screen text locally, send one frame per shot.

Everything about motion comes from ONE low-resolution 4 fps decode. The change score is ffmpeg's own
scene formula, min(mafd, |mafd - previous mafd|) / 100, computed in numpy. It replaced four ffmpeg passes
(freezedetect, scene, two caption strips) that took 50 s on a 143 s reel; the single decode took 12 s and
matched ffmpeg's detections: cuts 40/40, caption strip changes 176/176 and 80/81.

The scene-change idea and the greedy thumbnail dedup are adapted from bradautomates/claude-video
(skills/watch/scripts/frames.py), MIT licence. See THIRD_PARTY.md.
"""
import re
from difflib import SequenceMatcher

import numpy as np

from .common import run, run_bytes

FPS = 4
SMALL_W, SMALL_H = 180, 320
SCENE_THRESHOLD = 0.20     # matched ffmpeg's cuts 40/40 on the skit, 10/10 on synthetic tests
BAND_THRESHOLD = 0.03      # matched ffmpeg's caption-strip detections 176/176 and 80/81
BUSY_BAND_RATE = 1.0       # changes per second above which a strip is camera motion, not captions (live video hit 1.3-2.0/s)
FREEZE_MAFD = 1.0          # mean absolute grey difference per step below which nothing moved
FREEZE_MIN_STEPS = 4       # 1 s at 4 fps, like freezedetect d=1; isolated duplicate frames are not "static"
STATIC_RATIO = 0.90
STATIC_STD = 8             # pixels whose grey level varies less than this over the whole clip are overlay or border
BANDS = {"top": (0.05, 0.25), "bottom": (0.55, 0.30)}   # (y start, height) as fractions of frame height
OCR_WIDTH = 720
SEND_WIDTH = 504           # 504x896 = 18x32 tiles of 28 px = 576 Claude image tokens
MAX_CANDIDATES = 24
MERGE_WINDOW = 0.6
SAMPLE_EVERY = 15          # seconds; guarantees coverage of long uncut shots
SHOT_RESAMPLE = 40         # seconds; one camera shot still sends another frame this long after its last one.
                           # A 163 s in-car video merged into a single frame because the car interior never left the shot.
# A fade or black screen is dark AND flat. Measured on 221 real posts: true black frames had mean 0.0-1.0 and
# contrast (grey std) 0.0-0.3; every dark frame with content had std 6 or more ("someone still" on a starry sky:
# mean 2.3, std 11.0; a quote card: mean 3.5, std 24.8). Brightness alone would have dropped real content.
BLACK_LEVEL = 4
BLACK_STD = 3
DEDUP_THUMB = 16
DEDUP_THRESHOLD = 4.0      # repeats of a looped clip measured 0.2-0.3; the closest distinct shots measured 6.7
SAME_SHOT_INLIERS = 30     # ORB+RANSAC: same camera setup measured 82-266 inliers, different scenes 0-8
SAME_SHOT_MIN_SPREAD = 0.005   # inlier hull / frame area. The one wrong merge in 90 matching pairs (a faint watermark
                               # joining two poem scenes) was 0.002; every true same-shot pair was 0.007 or more. Thin margin.
PERSISTENT_EDGE_SHARE = 0.7    # a pixel that is an edge in this share of frames is overlay text, logo or border
TEXT_PROBE = 4             # text-change frames read first to decide whether the rest are worth reading
OCR_MIN_SCORE = 0.80
ARABIC_RE = re.compile(r"[؀-ۿ]")


# ---------- one decode, all motion signals ----------

def decode_small(video):
    raw = run_bytes(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf",
                     f"fps={FPS},scale={SMALL_W}:{SMALL_H},format=rgb24", "-f", "rawvideo", "-"])
    return np.frombuffer(raw, np.uint8).reshape(-1, SMALL_H, SMALL_W, 3)


def change_scores(stack):
    """ffmpeg's scene score between consecutive frames. Step i sits at time (i + 1) / FPS."""
    mafd = np.array([np.abs(stack[i + 1].astype(np.int16) - stack[i].astype(np.int16)).mean()
                     for i in range(len(stack) - 1)])
    if not len(mafd):
        return mafd, mafd
    prev = np.concatenate([[0.0], mafd[:-1]])
    return mafd, np.minimum(mafd, np.abs(mafd - prev)) / 100.0


def events(scores, threshold):
    return [(i + 1) / FPS for i, s in enumerate(scores) if s > threshold]


def frozen_ratio(grey_mafd, duration):
    frozen = run_len = 0
    for m in list(grey_mafd) + [np.inf]:
        if m < FREEZE_MAFD:
            run_len += 1
            continue
        if run_len >= FREEZE_MIN_STEPS:
            frozen += run_len
        run_len = 0
    return min(1.0, frozen / FPS / duration) if duration else 0.0


def moving_mask(grey):
    """255 where keypoints may be used, 0 on fixed overlays, watermarks and letterbox bars.

    Two kinds of overlay pixel are excluded: those whose grey level barely changes over the clip, and
    those that are an edge in most frames. The second catches text laid over a changing background, whose
    letter edges vary in brightness and so escape the first test: a fixed Arabic line merged 5 clips into
    2 shots with the static test alone, and was grouped correctly into 4 with both.
    """
    import cv2
    static = (grey.std(axis=0) < STATIC_STD).astype(np.uint8)
    edges = np.stack([cv2.Canny(g, 60, 150) > 0 for g in grey]).mean(axis=0) >= PERSISTENT_EDGE_SHARE
    excluded = cv2.dilate(np.maximum(static, edges.astype(np.uint8)), np.ones((5, 5), np.uint8))
    return np.where(excluded > 0, 0, 255).astype(np.uint8)


# ---------- moments ----------

def merge(events_, duration):
    """events_: (time, reason). Events within MERGE_WINDOW of a group's first event join that group.

    The group keeps its latest time (a caption that changes on a cut is only fully on screen a moment
    later), its highest-ranked reason, and whether any member was a text change. The window is anchored
    on the group's first event: anchoring on the latest time let dense events chain a clip into one moment.
    """
    rank = {"first": 0, "scene": 1, "sample": 2, "text": 3}
    groups = []   # [anchor, latest, reason, has_text]
    for t, reason in sorted(events_):
        t = min(max(t, 0.0), max(duration - 0.3, 0.0))
        if groups and t - groups[-1][0] < MERGE_WINDOW:
            g = groups[-1]
            g[1] = t
            g[3] = g[3] or reason == "text"
            if rank[reason] < rank[g[2]]:
                g[2] = reason
            continue
        groups.append([t, t, reason, reason == "text"])
    return [(latest, reason, has_text) for _, latest, reason, has_text in groups]


def even_sample(items, n):
    if len(items) <= n:
        return items
    if n <= 1:
        return items[:1]
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def extract_frame(video, t, out, width):
    for at in (t, max(t - 0.5, 0.0)):    # a seek right at the end can land past the last frame
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(out)])
        if out.exists():
            return True
    return False


# ---------- shot similarity ----------

def _thumb(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.convert("L").resize((DEDUP_THUMB, DEDUP_THUMB)).tobytes()


def _thumb_delta(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


_ORB = {}


def _features(path, mask_small):
    import cv2
    if "orb" not in _ORB:
        _ORB["orb"] = cv2.ORB_create(nfeatures=800)
        _ORB["matcher"] = cv2.BFMatcher(cv2.NORM_HAMMING)
    grey = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    mask = None
    if mask_small is not None:
        mask = cv2.resize(mask_small, (grey.shape[1], grey.shape[0]), interpolation=cv2.INTER_NEAREST)
    return _ORB["orb"].detectAndCompute(grey, mask)


def _match(a, b, shape):
    """(inliers, spread): keypoints one homography maps between two frames, and the area they cover.

    Inliers are high for the same camera setup and near zero otherwise. Spread guards against a small
    fixed mark the mask missed: a faint watermark gave 42 inliers packed into 0.2% of the frame.
    """
    import cv2
    (kp1, d1), (kp2, d2) = a, b
    if d1 is None or d2 is None or len(kp1) < 10 or len(kp2) < 10:
        return 0, 0.0
    pairs = _ORB["matcher"].knnMatch(d1, d2, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < 8:
        return 0, 0.0
    src = np.float32([kp1[m.queryIdx].pt for m in good])
    dst = np.float32([kp2[m.trainIdx].pt for m in good])
    _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if mask is None or mask.sum() < 3:
        return 0, 0.0
    hull = cv2.convexHull(src[mask.ravel() > 0])
    return int(mask.sum()), cv2.contourArea(hull) / (shape[0] * shape[1])


def _same_shot(a, b):
    inliers, spread = _match(a["feats"], b["feats"], a["shape"])
    return inliers >= SAME_SHOT_INLIERS and spread >= SAME_SHOT_MIN_SPREAD


def group_shots(candidates, mask_small, merge_shots):
    """Assign each candidate to a shot and choose the frames to keep. Returns (kept frames, number of shots).

    A new shot starts when a frame matches no earlier shot's first frame. Every shot keeps its first frame, and
    keeps another whenever SHOT_RESAMPLE seconds have passed since its last kept frame, so one long shot with a
    fixed background (a dashboard camera, a tripod) is not reduced to a single frame. `shot_first` marks kept frames.
    """
    reps, kept, last_kept = [], [], {}
    for c in candidates:
        c["thumb"] = _thumb(c["ocr_path"])
        if merge_shots:
            from PIL import Image
            c["feats"] = _features(c["ocr_path"], mask_small)
            with Image.open(c["ocr_path"]) as im:       # header only; the frame is not decoded twice
                c["shape"] = (im.height, im.width)
        for i, rep in enumerate(reps):
            if _thumb_delta(c["thumb"], rep["thumb"]) <= DEDUP_THRESHOLD or (merge_shots and _same_shot(c, rep)):
                c["shot"] = i
                c["shot_first"] = c["t"] - last_kept[i] >= SHOT_RESAMPLE
                if c["shot_first"]:
                    kept.append(c)
                    last_kept[i] = c["t"]
                break
        else:
            c["shot"], c["shot_first"] = len(reps), True
            last_kept[len(reps)] = c["t"]
            reps.append(c)
            kept.append(c)
    return kept, len(reps)


# ---------- OCR ----------

OCR_THREADS = None         # a batch runner may set this so parallel workers share the CPU instead of each claiming all of it
_ENGINES = {}


def _engine(kind):
    """ar: detection + Arabic recognition. ar_rec / en: recognition only on a crop.

    Separate instances because RapidOCR remembers use_det=False after one call. The detector stays the
    default PP-OCRv6 small: the v5 and v4 mobile detectors were only 17% faster and broke Arabic lines.
    """
    if kind not in _ENGINES:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
        params = {"Global.log_level": "error"}
        if OCR_THREADS:
            params.update({"EngineConfig.onnxruntime.intra_op_num_threads": OCR_THREADS,
                           "EngineConfig.onnxruntime.inter_op_num_threads": 1})
        if kind in ("ar", "ar_rec"):
            params.update({"Rec.lang_type": LangRec.ARABIC, "Rec.ocr_version": OCRVersion.PPOCRV5,
                           "Rec.model_type": ModelType.MOBILE, "Det.unclip_ratio": 2.0})
        _ENGINES[kind] = RapidOCR(params=params)
    return _ENGINES[kind]


def _recognise(kind, crop):
    if not crop.size:
        return "", 0.0
    r = _engine(kind)(crop, use_det=False, use_cls=False)
    return (r.txts[0], float(r.scores[0])) if r.txts and r.scores else ("", 0.0)


def ocr(path):
    import cv2
    img = cv2.imread(str(path))
    if img is None:
        return []
    height, width = img.shape[:2]
    res = _engine("ar")(img, use_det=True, use_cls=True)
    if res.boxes is None or res.txts is None:
        return []
    lines = []
    for box, text, score in zip(res.boxes, res.txts, res.scores):
        ys, xs = [pt[1] for pt in box], [pt[0] for pt in box]
        y0, y1 = max(int(min(ys)), 0), int(max(ys)) + 1
        if ARABIC_RE.search(text):
            # The detector can clip a short word at the line's edge (it dropped a leading "مو" in testing,
            # a negation). Re-reading a crop widened by half the line height recovered it.
            grow = int(0.5 * (y1 - y0))
            wide, wide_score = _recognise("ar_rec", img[y0:y1, max(int(min(xs)) - grow, 0):min(int(max(xs)) + grow, width)])
            if len(wide) > len(text) and wide_score >= score - 0.02:
                text, score = wide, wide_score
        else:
            # The Arabic recogniser is weak on Latin script; let the general model read that box.
            en, en_score = _recognise("en", img[y0:y1, max(int(min(xs)), 0):int(max(xs)) + 1])
            if en_score > score:
                text, score = en, en_score
        text = text.strip()
        # Two-character Latin fragments on live video were noise ("F4", "80"); a short Arabic word can be a negation.
        if score < OCR_MIN_SCORE or len(text) < 2 or (len(text) < 3 and not ARABIC_RE.search(text)):
            continue
        y = sum(ys) / len(ys) / height
        lines.append({"text": text, "score": round(float(score), 2), "y": y, "x": min(xs)})
    lines.sort(key=lambda l: (round(l["y"], 2), -l["x"]))
    return lines


def band_of(y):
    return "top" if y < 0.30 else "bottom" if y > 0.55 else "middle"


def _is_repeat(norm, seen):
    """A watermark read five ways ("copyrigh", "copyri", "copjigh") is still one line."""
    for s in seen:
        if SequenceMatcher(None, norm, s).ratio() >= 0.8:
            return True
        if min(len(norm), len(s)) >= 4 and (norm in s or s in norm):
            return True
    return False


def _collect_text(path, t, label, onscreen, seen):
    """OCR one frame into onscreen; returns how many lines were new."""
    new = 0
    for line in ocr(path):
        norm = re.sub(r"\s+", " ", line["text"]).lower()
        if _is_repeat(norm, seen):
            continue
        seen.append(norm)
        onscreen.append({"t": t, "label": label, "text": line["text"], "band": band_of(line["y"])})
        new += 1
    return new


def _send_copy(src, out):
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.resize((SEND_WIDTH, max(round(im.height * SEND_WIDTH / im.width / 2) * 2, 2))).save(out, quality=85)


def _clean(frames_dir):
    frames_dir.mkdir(exist_ok=True)
    for old in frames_dir.glob("*.jpg"):
        old.unlink()


# ---------- stage entry points ----------

def analyse(video, duration, workdir, max_frames, decisions, merge_shots=True):
    frames_dir = workdir / "frames"
    _clean(frames_dir)

    small = decode_small(video)
    grey = (small[..., 0] * 0.299 + small[..., 1] * 0.587 + small[..., 2] * 0.114).astype(np.uint8)
    grey_mafd, _ = change_scores(grey[..., None])
    ratio = frozen_ratio(grey_mafd, duration)

    text_events, busy = [], {}
    for name, (y, h) in BANDS.items():
        _, band_scores = change_scores(grey[:, int(SMALL_H * y):int(SMALL_H * (y + h)), :, None])
        times = events(band_scores, BAND_THRESHOLD)
        rate = len(times) / duration if duration else 0.0
        if rate > BUSY_BAND_RATE:
            # Too fast to pick OCR frames from (camera motion would flood them), but kept as a hint for Claude:
            # motion gags, lyric videos and kinetic text all change this fast, and throwing them away lost a gag
            # whose joke was the motion and a song written on screen.
            busy[name] = times
            decisions.append(f"video: {name} strip changed {len(times)} times ({rate:.1f}/s), too fast to read as captions")
            continue
        text_events += [(t, "text") for t in times]

    static = ratio >= STATIC_RATIO and not text_events
    cut_times = []
    if static:
        moments = [(duration / 2, "first", False)]
        cuts = 0
        decisions.append(f"video: static ({ratio:.0%} frozen, no text changes), one frame only")
    else:
        _, scene_scores = change_scores(small)
        cut_times = events(scene_scores, SCENE_THRESHOLD)
        cuts = [(t, "scene") for t in cut_times]
        # A long moving shot has no cuts; one sample per SAMPLE_EVERY seconds keeps it from reducing to its first frame.
        samples = [(float(t), "sample") for t in range(SAMPLE_EVERY, int(duration), SAMPLE_EVERY)]
        if not samples and duration >= 3:
            # A short clip with no cuts was judged on its t=0 frame alone, which was a black fade-in on a real reel.
            samples = [(duration / 2, "sample")]
        moments = merge([(0.0, "first"), *cuts, *samples, *[(t + 0.3, r) for t, r in text_events]], duration)
        moments = even_sample(moments, MAX_CANDIDATES)
        cuts = len(cuts)
        decisions.append(f"video: {ratio:.0%} frozen, {cuts} cuts, {len(text_events)} text changes, "
                         f"{len(moments)} moments checked")

    candidates = []
    for i, (t, reason, has_text) in enumerate(moments):
        path = frames_dir / f"ocr_{i:03d}.jpg"
        if extract_frame(video, t, path, OCR_WIDTH):
            candidates.append({"t": t, "reason": reason, "has_text": has_text, "ocr_path": path})

    def is_lit(c):
        from PIL import Image, ImageStat
        with Image.open(c["ocr_path"]) as im:
            stat = ImageStat.Stat(im.convert("L"))
        return stat.mean[0] >= BLACK_LEVEL or stat.stddev[0] >= BLACK_STD

    lit = [c for c in candidates if is_lit(c)]
    if lit and len(lit) < len(candidates):
        # Fades and black screens carry nothing to describe; keep them only when the whole clip is black.
        for c in candidates:
            if c not in lit:
                c["ocr_path"].unlink(missing_ok=True)
        decisions.append(f"video: {len(candidates) - len(lit)} near-black frames dropped")
        candidates = lit
    if candidates and not candidates[0]["reason"] == "first":
        candidates[0]["reason"] = "first"

    mask = None if static else moving_mask(grey)
    reps, n_shots = group_shots(candidates, mask, merge_shots)

    # OCR: the first frame of every shot, then a probe of text-change frames. Only if the probe finds new
    # text in at least half its frames are the remaining text-change frames read. On the skit, 81 "text
    # changes" were hair and hands moving through the top strip; reading all of them cost most of the stage.
    onscreen, seen = [], []
    for c in reps:
        _collect_text(c["ocr_path"], c["t"], None, onscreen, seen)
    pending = [c for c in candidates if c["has_text"] and not c["shot_first"]]
    probe = even_sample(pending, TEXT_PROBE)
    hits = sum(_collect_text(c["ocr_path"], c["t"], None, onscreen, seen) > 0 for c in probe)
    commit = bool(probe) and hits * 2 >= len(probe)
    rest = [c for c in pending if c not in probe] if commit else []
    for c in rest:
        _collect_text(c["ocr_path"], c["t"], None, onscreen, seen)
    onscreen.sort(key=lambda o: o["t"])
    probe_note = (f"; text probe found new text in {hits} of {len(probe)}, "
                  f"{'read the other ' + str(len(rest)) if commit else 'skipped the other ' + str(len(pending) - len(probe))}"
                  if probe else "")
    decisions.append(f"ocr: {len(reps) + len(probe) + len(rest)} of {len(candidates)} frames read{probe_note}")

    chosen = even_sample(reps, max_frames)
    if not static:
        grouping = "shots (ORB)" if merge_shots else "distinct frames (--dense)"
        decisions.append(f"frames: {len(candidates)} moments form {n_shots} {grouping}, {len(reps)} kept, {len(chosen)} sent")

    sent = []
    for i, c in enumerate(chosen):
        out = frames_dir / f"frame_{i:02d}_{int(c['t'] * 10):04d}.jpg"
        _send_copy(c["ocr_path"], out)
        sent.append({"t": c["t"], "label": None, "reason": c["reason"], "path": str(out)})
    for c in candidates:
        c["ocr_path"].unlink(missing_ok=True)

    # Claude navigates the video itself (look.py). The overview starts it on the detected moments: one cell per kept
    # shot frame, each text change, a few samples of each constantly changing strip, and fillers so no stretch longer
    # than an eighth of the video goes unseen. Scattered detected moments already tell what the whole post is.
    from . import frames as frame_store
    cells = [(c["t"], "shot") for c in reps]
    cells += [(min(t + 0.3, max(duration - 0.1, 0.0)), "text") for t, _ in text_events]
    for name, times in busy.items():
        cells += [(t, f"{name} busy") for t in even_sample(times, 4)]
    points = []
    for t, why in sorted(cells):
        if not points or t - points[-1][0] >= 0.5:
            points.append((t, why))
    max_gap = max(duration / 8, 1.0)
    edges = [0.0] + [t for t, _ in points] + [duration]
    fills = []
    for a, b in zip(edges, edges[1:]):
        n = int((b - a) // max_gap)
        fills += [(a + (b - a) * (k + 1) / (n + 1), "fill") for k in range(n)]
    overview = [{"t": round(t, 2), "reason": why} for t, why in even_sample(sorted(points + fills), 16)]
    frame_store.extract_all(video, workdir)
    sheet = frame_store.make_sheet(workdir, [(c["t"], c["reason"]) for c in overview], frames_dir / "overview.jpg")
    timeline = {"cuts": cut_times, "text_changes": [t for t, _ in text_events], "busy": busy}

    return {"static_ratio": ratio, "static": static, "cuts": cuts, "text_changes": len(text_events),
            "shots": n_shots, "frames": sent, "onscreen": onscreen,
            "timeline": timeline, "overview": {"path": str(sheet), "cells": overview}}


def analyse_images(images, workdir, max_frames, decisions):
    """Photo posts: every image is OCR'd, up to max_frames are sent."""
    import cv2
    frames_dir = workdir / "frames"
    _clean(frames_dir)
    onscreen, seen, prepared = [], [], []
    for n, src in enumerate(images, 1):
        img = cv2.imread(str(src))
        if img is None:
            continue
        h, w = img.shape[:2]
        if w > OCR_WIDTH:
            img = cv2.resize(img, (OCR_WIDTH, round(h * OCR_WIDTH / w)))
        path = frames_dir / f"ocr_{n:02d}.jpg"
        cv2.imwrite(str(path), img)
        _collect_text(path, 0.0, f"image {n}", onscreen, seen)
        prepared.append((n, path))

    sent = []
    for n, path in even_sample(prepared, max_frames):
        out = frames_dir / f"image_{n:02d}.jpg"
        _send_copy(path, out)
        sent.append({"t": 0.0, "label": f"image {n}", "reason": "image", "path": str(out)})
    for _, path in prepared:
        path.unlink(missing_ok=True)
    decisions.append(f"photo post: {len(images)} images, all OCR'd, {len(sent)} sent")
    return {"static_ratio": 1.0, "static": True, "cuts": 0, "text_changes": 0, "shots": len(sent),
            "frames": sent, "onscreen": onscreen}
