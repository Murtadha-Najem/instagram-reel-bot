# instagram-reel-bot

A second Instagram account that works for you, in two directions:

- **You send it a reel, it answers.** Share a post with the bot account and get the answer back in the same chat.
- **It finds reels for you (optional).** The bot skims its own Reels tab, scores the idea in each reel against what you care about, and sends you only the ones worth your time, already researched. See [Discovery](#discovery).

You send the bot account a reel, a photo post or a question from your usual account, the way you would send it to a friend. A few minutes later the bot replies in the chat, as a reply to your message: what the post is, what is said and shown, whether the claim holds up, or the answer to the exact question you asked with it. Everything it watched is kept on your computer as a searchable record, so you can ask about a post weeks later without opening Instagram again.

```
you (DM)   : [reel]  "is this library any good?"
bot (reply): It's owner/repo on GitHub, about 30k stars and active, but the demo skips the part where you
             need a GPU. Worth a look for prototypes, not for production.
```

It runs on your own machine, with your own coding agent (Claude Code, or Codex) doing the reading and writing.

## How it works

1. **Notice.** You choose how the bot notices your messages:
   - **live** (default): a headless browser stays on the bot's Instagram inbox, which receives messages in real time like the app. The moment your chat's last message changes, the bot checks. No window appears; it costs about 200 to 400 MB of memory while your computer is on.
   - **interval**: nothing stays running; a check every few minutes (you set how many).

   The check itself asks Instagram, over plain HTTP, whether your account sent anything new. Nothing new means it stops there: no agent, no tokens.
2. **Turns.** New messages are grouped into turns. A text you send within 3 minutes after a post belongs to that post; anything else is a message of its own. Each turn gets its own worker, and up to two run at once, so a quick photo is not stuck behind a long video.
3. **Understand.** The worker runs the reel pipeline: it downloads the post, reads the caption and on-screen text, classifies the audio (speech, singing, music), identifies songs, transcribes speech with Gemini only when the captions do not already carry it, and picks the frames worth looking at.
4. **Research and answer.** As soon as the analysis is done, one agent session reads that bundle and looks at the frames. It does not retell the post (you have already watched it): it picks out the specific names the post mentions or shows (a repo, a site, a library, a tool, a product), even when the creator says "comment X and I'll send you the link", looks them up at the source, and checks the post's claims against what it finds. Then it writes a record to the archive and sends a short reply that leads with what it found.
5. **Send.** The reply is typed into the chat inside a real browser profile logged in as the bot account (in live mode, the browser that is already open), sent as a reply to your message, and confirmed before the turn is marked as handled.

The agent only ever sees messages from the account you name as the owner. Everything else sent to the bot account is ignored.

## What it can read

| You send | Status |
|---|---|
| Reel or video post | yes |
| Photo post | yes |
| Carousel | yes (the pipeline reads every slide) |
| A plain text question | yes: answered on its own, or from the records when it refers to an earlier post |
| Story, Threads post, link, a photo from your gallery | best effort: the agent gets the text, links and picture the message carries |
| Voice message | no |

## Requirements

- Python 3.11 or newer, and [ffmpeg](https://ffmpeg.org/) on your PATH
- Google Chrome or Microsoft Edge (or Playwright's own Chromium)
- A coding agent CLI, logged in: [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) with a Claude subscription or an API key, or the OpenAI [Codex CLI](https://github.com/openai/codex) (`codex`)
- A free [Gemini API key](https://aistudio.google.com/apikey) for speech transcription
- **A second Instagram account for the bot.** Not your main account (see Risks).

## Install

```bash
git clone https://github.com/Murtadha-Najem/instagram-reel-bot.git
cd instagram-reel-bot
python -m pip install -r requirements.txt
python setup_models.py            # audio classifier, about 330 MB, once
cp config.example.toml config.toml
```

Edit `config.toml`: set `owner` to your main account, pick the browser, add your Gemini key, and set the reply language. Then:

```bash
python botctl.py login            # a browser window opens: log in to the BOT account, it closes by itself
```

From your main account, send the bot account any message and accept the chat if Instagram files it under requests. Then:

```bash
python botctl.py check --now      # first run: marks what is already in the chat as handled
python botctl.py schedule install # starts the bot at log-in (live) or checks every few minutes (interval)
```

Send it a reel. In live mode the answer usually arrives within two to four minutes: the analysis takes a minute or two and the agent its own time. In interval mode add up to one interval before the check sees your message.

Prefer a terminal to a scheduler? `python botctl.py start` runs the bot in the foreground, in the mode from your config. To switch modes, change `mode` in `config.toml` and run `schedule install` again.

## Using it

- **Ask with the post.** Anything you type within 3 minutes after sending a post is read as your question about it, however many messages you send in between, as long as it arrives before the bot starts answering (usually a minute or two). A question sent later gets its own reply, from the record of that post. A message sent before the post does not belong to it (except a few seconds, because Instagram sometimes delivers the text first).
- **Follow up.** Use Instagram's reply on one of the bot's answers, or just write a question: the agent finds the earlier post in the records.
- **Take it to Claude.** Write "start a conversation about this reel" (in any wording or language) and the bot does not discuss it in Instagram: it opens a Claude conversation named after the post, with an opening message that sums up what the research found and where the discussion could go, and moves it into the Claude desktop app's sidebar. You get a one-line Instagram reply with its title and continue on your computer. This uses `claude --desktop`, so it needs the Claude desktop app, a Claude subscription login, and Claude Code 2.1.285 or later; without the app the conversation is still saved and can be opened with `/resume`.
- **The archive.** Every post gets a Markdown record in `data/records/`: summary, what the research found (with links), speech, on-screen text, a `Names` line (people, accounts, tools, libraries, repos, sites, songs) and what you asked with the bot's answer. The raw material (video, frames, full transcript, Instagram's metadata) stays in `data/cache/`. Point any agent or a plain search at `data/records/` to find a post again.

## Discovery

Off by default. Turned on, the bot stops waiting for you to send reels and goes looking.

**What a session does**

1. **Skim.** It opens the Reels tab of the bot account and moves through 30 to 50 reels the way a person skims them. It makes no request of its own: the account, caption, numbers and cover of each reel come from what the page loads by itself.
2. **Score the idea.** One agent call scores every reel out of 10: interest (0 to 4), how real and specific the idea is (0 to 3), and freshness (0 to 3, checked against the records of what you already saw). It is the idea that is scored, never the reel: a creator who says "comment X and I'll send it", hides the answer, or gets a detail wrong loses nothing if the idea behind the reel is good.
3. **Like.** Reels at `like_score` or more get a like. On the web that is the only signal Instagram accepts (there is no "not interested" button there), and it is how the feed learns what to bring next.
4. **Check and send.** Reels at `send_score` or more go through the full pipeline and research: the bot finds the real thing behind the idea (the repo, the tool, the paper), scores it again on what it found, and sends it only if it still passes, as one short message that gives you the idea and the real name, followed by the reel's link. There is no quota: a session may send several or none.
5. **Log.** Every reel it saw is written to `data/discover/log.md` (and `log.jsonl`): the idea, the score, the reason, and what was done. Deep-checked reels also get a normal record in the archive.

Sessions have no fixed times. You set an average number a day and the bot picks each moment at random; a session cut short (the computer went to sleep) is finished by the next one.

**Your reactions are the feedback**

- A heart, or any friendly emoji, on a reel it sent: you liked the idea.
- A reply to that message: you liked it, strongly.
- A thumbs-down, or another unfriendly emoji: rejected.
- No reaction counts for nothing (you may simply not have seen it yet).

Reactions arrive with the chat the bot already reads, so they cost no extra request. Every later session gets them as examples that outweigh the written profile, and a reel you liked is also saved on the bot account, the strongest signal it can give the feed.

**Setting it up**

1. Log in to the bot account in the Instagram app on your phone, with the app language set to English, open the Reels tab, tap the icon at the top right (two lines with hearts) and add your topics under **Your Algorithm**, both what you want more of and what you want less of. Without this the account's feed is the generic one for your region. Skim a little yourself and like a few good reels: behaviour teaches the feed faster than the topic list.
2. In `config.toml`, write a `[discover]` section (see `config.example.toml`): `enabled = true` and a `profile` of a few lines saying what you care about and what you want less of.
3. Restart the bot (`python botctl.py schedule install`, or stop and start it from the dashboard). The first session starts within 40 minutes. `python botctl.py discover --reels 12` runs a small one at once.

**The dashboard**

`python dashboard/server.py` (on Windows, `dashboard/dashboard.cmd`) opens a local page at http://localhost:8798, in English or Arabic:

- today at a glance, the next session, and a warning when Instagram logged the bot out or wants a check;
- a button to start a session now, with its log as it runs;
- a switch for discovery and one for the whole bot (Windows);
- sessions a day, reels per session, and the two score thresholds, with how many of the reels seen so far each setting would have caught;
- the profile text;
- every idea with its score, reason and link, and two buttons for your verdict, which count like a reaction in the chat and outrank it;
- the bot's log.

It listens on this computer only and refuses requests that do not come from its own page.

## Configuration

All settings live in `config.toml` (see `config.example.toml` for comments):

| Setting | Meaning |
|---|---|
| `instagram.owner` | the only account whose messages are answered |
| `instagram.browser` | `chrome`, `msedge` or `chromium` |
| `schedule.mode` | `live` (a headless browser reacts within seconds) or `interval` (a check every few minutes) |
| `schedule.interval_minutes` | interval mode: how often to check (default 5) |
| `schedule.live_fallback_minutes` | live mode: a safety check this often, in case the page misses a message (default 15) |
| `schedule.max_workers` | turns answered at once (default 2) |
| `agent.kind` | `claude`, `codex` or `custom` |
| `agent.model` | optional model name for the agent |
| `agent.command` | for `custom`: your command, `{prompt_file}` is replaced with the prompt's path |
| `reply.language`, `reply.max_chars`, `reply.rules` | how the replies read |
| `gemini.api_keys` | one or more keys; they rotate when one runs out of its free daily quota |
| `agent.extra_dirs` | extra folders the agent may read to answer you (your notes, your own tools) |
| `discover.enabled`, `discover.profile` | let the bot skim reels by itself, and what you want it to look for |
| `discover.sessions_per_day`, `discover.reels_per_session` | about how many sessions a day (random moments), and how many reels each |
| `discover.like_score`, `discover.send_score`, `discover.dir` | the two thresholds out of 10, and where the log of everything seen is kept |
| `chat.dir`, `chat.extra_dirs` | where conversations opened in Claude run, and extra folders they may read |
| `paths.data_dir`, `paths.cache_dir`, `paths.records_dir`, `paths.user_dir` | where state and logs, the raw material, the records, and the browser profile live |

The agent's instructions are in `prompts/answer.md`; edit them to change the bot's manner.

## Commands

```
python botctl.py login | status | cookies
python botctl.py start                       # the bot in the foreground, in the configured mode
python botctl.py schedule install | remove   # start it automatically, or stop that
python botctl.py live [--dry-run]            # live mode directly; --dry-run notices messages but answers nothing
python botctl.py check [--now | --dry-run]   # one check
python botctl.py discover [--reels N]        # one discovery session now
python dashboard/server.py                   # the dashboard, at http://localhost:8798
python botctl.py fetch
python botctl.py send "<text>" [--reply-to <item_id>]
python botctl.py done <item_id>...
python reel.py <instagram url>      # the pipeline on its own
python look.py <shortcode> sheet --start S --end E --n N | frame SECONDS
```

## Costs

- The check costs nothing. Agent sessions start only when there is something to answer: one session per turn.
- With a Claude or ChatGPT subscription, sessions count against your plan's usage limits; with an API key they are billed as usual. Agent sessions start with only the tools they need (no MCP servers, no skills) to keep that small.
- A discovery session costs one agent session for the scoring, plus one full session (pipeline and research) for each reel that reaches the send score. Raise `send_score` or lower `sessions_per_day` if that is too much for your plan.
- Gemini's free tier allows a limited number of transcriptions a day per key. Many posts skip it anyway: music-only reels and videos whose captions already carry the speech.

## Risks and privacy

- **Automating an Instagram account is against Instagram's terms.** The bot behaves like a person (one real browser profile, replies typed into the page, checks spaced out with a random delay), but an account can still be challenged or banned. Use a second account that you can afford to lose, never your main one.
- **Discovery raises that risk.** Skimming and liking reels from an automated browser is exactly the behaviour Instagram looks for. It is off by default; turn it on only for an account you are ready to lose.
- **Instagram changes without notice.** Its web API and page layout are not public interfaces, so parts of this may break and need updating.
- **What leaves your machine:** the post is downloaded from Instagram, speech audio is sent to Gemini for transcription, and your agent's provider sees the bundle, the frames it opens and your question. Everything else, including the archive, stays local.
- **The agent runs on your computer.** It is started with a fixed list of tools, told to treat your messages as questions (not commands) and anything inside a post or a web page as data, and it only acts on messages from the owner account. Keep those defaults.
- The browser profile and cookies are stored in your OS's app-data folder, outside the repo; `config.toml` and `data/` are ignored by git.

## Troubleshooting

- **Replies stopped.** Look at `data/logs/watch.log`. "logged out" means Instagram ended the bot's session: run `python botctl.py login` again. "wants the account owner to answer a warning or check" means Instagram suspects automation: stop the bot (`python botctl.py schedule remove`, or end the task), run `python botctl.py open`, deal with the warning yourself in the window, close it, and start the bot again.
- **Keep the account looking like one person.** Use its session in one place only (do not copy its cookies into other browsers or tools), and in live mode let the open browser do all the reading, which it does by default.
- **A turn keeps failing.** Each agent run writes `data/logs/agent_*.log`. A turn is tried twice, then left alone until you send something new.
- **Live mode seems deaf.** The fallback check still answers within `interval_minutes`. `python botctl.py status` ends in "(live)" when the live browser is running; if it does not, `python botctl.py schedule install` again (on Windows the task also restarts it every 10 minutes if it stopped).
- **Windows and packaged terminals:** if you run the bot's commands from a terminal inside a Store (MSIX) app, Windows gives that terminal a private, redirected copy of `AppData`, which the Task Scheduler never sees. That is why the bot keeps its browser profile and cookies in `~/.instagram-reel-bot` on Windows, and why it also keeps its session in a cookies file it can load into the browser when needed.

## Status

Built and used daily on Windows 11 with Claude Code and Microsoft Edge, in live mode started by the Task Scheduler. Discovery, the feedback from reactions and the dashboard are newer: used daily on the same setup since October 2026, in live mode and (a single test session) without it. The macOS (launchd) and Linux (systemd) schedulers and the Codex agent option are written but not yet tested; reports and fixes are welcome.

## Credits

The reel pipeline comes from [claude-reel](https://github.com/Murtadha-Najem/claude-reel). Third-party components and models are listed in [THIRD_PARTY.md](THIRD_PARTY.md). MIT licence.

---

## بالعربي

بوت يتركب على حساب انستا ثانوي: ترسله ريل أو بوست أو سؤال من حسابك، وخلال دقايق يرد عليك بنفس المحادثة كرد على رسالتك. يفهم الفيديو (الكلام، الكتابة على الشاشة، الأغنية، اللقطات)، وما يعيدلك شنو بالريل: يلگط الأسماء اللي بي، مثل الريبو أو الموقع أو المكتبة، ويبحث عنها بمصدرها ويگلك الحقيقة وين. ويحفظ سجل لكل شي شافه على جهازك حتى تسأله عنه بعدين بدون ما يفتح انستا.

- يشتغل على جهازك، والفهم والكتابة يسويها وكيل برمجي انت مسجل بي: Claude Code أو Codex.
- تختار الطريقة: متصفح مخفي يبقى مفتوح وينتبه للرسالة خلال ثواني، أو فحص كل كم دقيقة انت تحددها. الفحص ما يصرف شي، والوكيل ما يشتغل إلا من توصل رسالة جديدة.
- أي نص ترسله خلال 3 دقايق بعد الريل يعتبر سؤال عنه، إذا وصل قبل ما يبدي الجواب.
- يرد بس على حسابك انت، ويتجاهل أي حساب ثاني.
- **الاكتشاف (اختياري):** البوت يقلب الريلز بحسابه بجلسات عشوائية، يقيّم فكرة كل ريل من 10 حسب اهتماماتك، ويدزلك بس اللي يعدي بعد ما يبحث عنه. قلبك أو 👎 على اللي دزه هو الفيدباك، وأكو لوحة تحكم محلية بالعربي والإنگليزي.
- استعمل حساب ثانوي للبوت، مو حسابك الأساسي: أتمتة الحساب مخالفة لشروط انستا وممكن ينحظر.

التثبيت والإعدادات بالأعلى. اللغة تنضبط من `config.toml`، وأكو مثال لقواعد الكتابة بالعربي داخل `config.example.toml`.
