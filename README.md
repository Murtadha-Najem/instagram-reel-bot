# instagram-reel-bot

Share a reel with a second Instagram account and get the answer back in the same chat.

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
- **The archive.** Every post gets a Markdown record in `data/records/`: summary, what the research found (with links), speech, on-screen text, a `Names` line (people, accounts, tools, libraries, repos, sites, songs) and what you asked with the bot's answer. The raw material (video, frames, full transcript, Instagram's metadata) stays in `data/cache/`. Point any agent or a plain search at `data/records/` to find a post again.

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
| `paths.data_dir`, `paths.cache_dir`, `paths.records_dir`, `paths.user_dir` | where state and logs, the raw material, the records, and the browser profile live |

The agent's instructions are in `prompts/answer.md`; edit them to change the bot's manner.

## Commands

```
python botctl.py login | status | cookies
python botctl.py start                       # the bot in the foreground, in the configured mode
python botctl.py schedule install | remove   # start it automatically, or stop that
python botctl.py live [--dry-run]            # live mode directly; --dry-run notices messages but answers nothing
python botctl.py check [--now | --dry-run]   # one check
python botctl.py fetch
python botctl.py send "<text>" [--reply-to <item_id>]
python botctl.py done <item_id>...
python reel.py <instagram url>      # the pipeline on its own
python look.py <shortcode> sheet --start S --end E --n N | frame SECONDS
```

## Costs

- The check costs nothing. Agent sessions start only when there is something to answer: one session per turn.
- With a Claude or ChatGPT subscription, sessions count against your plan's usage limits; with an API key they are billed as usual. Agent sessions start with only the tools they need (no MCP servers, no skills) to keep that small.
- Gemini's free tier allows a limited number of transcriptions a day per key. Many posts skip it anyway: music-only reels and videos whose captions already carry the speech.

## Risks and privacy

- **Automating an Instagram account is against Instagram's terms.** The bot behaves like a person (one real browser profile, replies typed into the page, checks spaced out with a random delay), but an account can still be challenged or banned. Use a second account that you can afford to lose, never your main one.
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

Built and used daily on Windows 11 with Claude Code and Microsoft Edge, in live mode started by the Task Scheduler. The macOS (launchd) and Linux (systemd) schedulers and the Codex agent option are written but not yet tested; reports and fixes are welcome.

## Credits

The reel pipeline comes from [claude-reel](https://github.com/Murtadha-Najem/claude-reel). Third-party components and models are listed in [THIRD_PARTY.md](THIRD_PARTY.md). MIT licence.

---

## بالعربي

بوت يتركب على حساب انستا ثانوي: ترسله ريل أو بوست أو سؤال من حسابك، وخلال دقايق يرد عليك بنفس المحادثة كرد على رسالتك. يفهم الفيديو (الكلام، الكتابة على الشاشة، الأغنية، اللقطات)، وما يعيدلك شنو بالريل: يلگط الأسماء اللي بي، مثل الريبو أو الموقع أو المكتبة، ويبحث عنها بمصدرها ويگلك الحقيقة وين. ويحفظ سجل لكل شي شافه على جهازك حتى تسأله عنه بعدين بدون ما يفتح انستا.

- يشتغل على جهازك، والفهم والكتابة يسويها وكيل برمجي انت مسجل بي: Claude Code أو Codex.
- تختار الطريقة: متصفح مخفي يبقى مفتوح وينتبه للرسالة خلال ثواني، أو فحص كل كم دقيقة انت تحددها. الفحص ما يصرف شي، والوكيل ما يشتغل إلا من توصل رسالة جديدة.
- أي نص ترسله خلال 3 دقايق بعد الريل يعتبر سؤال عنه، إذا وصل قبل ما يبدي الجواب.
- يرد بس على حسابك انت، ويتجاهل أي حساب ثاني.
- استعمل حساب ثانوي للبوت، مو حسابك الأساسي: أتمتة الحساب مخالفة لشروط انستا وممكن ينحظر.

التثبيت والإعدادات بالأعلى. اللغة تنضبط من `config.toml`، وأكو مثال لقواعد الكتابة بالعربي داخل `config.example.toml`.
