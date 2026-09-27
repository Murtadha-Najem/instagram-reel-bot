"""Download the audio classifier (PANNs CNN14, about 330 MB) and its labels into ~/panns_data.

panns-inference fetches these itself with wget, which most Windows machines lack. Run this once instead.
Without the model the pipeline still works, but cannot tell speech from music and so calls Gemini more often.
"""
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from reel.audio import PANNS_CKPT, PANNS_DIR, PANNS_LABELS  # noqa: E402

FILES = {
    PANNS_CKPT: "https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth?download=1",
    PANNS_LABELS: "https://raw.githubusercontent.com/qiuqiangkong/audioset_tagging_cnn/master/metadata/class_labels_indices.csv",
}


def main():
    PANNS_DIR.mkdir(parents=True, exist_ok=True)
    for path, url in FILES.items():
        if path.exists() and path.stat().st_size > 0:
            print(f"already there: {path}")
            continue
        print(f"downloading {path.name} ...")
        tmp = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(path)
        print(f"saved {path} ({path.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
