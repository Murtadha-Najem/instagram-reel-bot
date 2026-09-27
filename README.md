# instagram-reel-bot

Share a reel with a second Instagram account and get the answer back in the same chat.

You send the bot account a reel, a photo post or a question from your usual account, the way you would send it to a friend. A few minutes later the bot replies in the chat, as a reply to your message: what the post is, what is said and shown, whether the claim holds up, or the answer to the exact question you asked with it. Everything it watched is kept on your computer as a searchable record, so you can ask about a post weeks later without opening Instagram again.

```
you (DM)   : [reel]  "is this library any good?"
bot (reply): It's a real open-source project with about 30k stars, but the demo skips the part where you
             need a GPU. Worth a look for prototypes, not for production.
```

It runs on your own machine, with your own coding agent (Claude Code, or Codex) doing the reading and writing.

## How it works

1. **Check.** Every few minutes a scheduled check asks Instagram, over plain HTTP, whether your account sent the bot anything new. Nothing new means it stops there: no browser, no agent, no tokens.
2. **Turns.** New messages are grouped into turns. A text you send within 3 minutes after a post belongs to that post; anything else is a message of its own. Each turn gets its own worker, and up to two run at once, so a quick photo is not stuck behind a long video.
3. **Understand.** The worker runs the reel pipeline: it downloads the post, reads the caption and on-screen text, classifies the audio (speech, singing, music), identifies songs, transcribes speech with Gemini only when the captions do not already carry it, and picks the frames worth looking at.
4. **Answer.** One agent session reads that bundle, looks at the frames, checks facts on the web if your question needs it, writes a record to the archive, and sends a short reply.
5. **Send.** The reply is typed into the chat inside a real browser profile logged in as the bot account, sent as a reply to your message, and confirmed before the turn is marked as handled.

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
python botctl.py schedule install # checks every few minutes from now on
```

Send it a reel. The first answer usually arrives within five to ten minutes: up to one interval until the check sees it, a minute or two of analysis, and the agent's own time.

No scheduler, or you prefer a terminal? `python botctl.py run` checks forever in the foreground.

## Using it

- **Ask with the post.** Anything you type within 3 minutes after sending a post is read as your question about it, however many messages you send in between. A message sent before the post does not belong to it (except a few seconds, because Instagram sometimes delivers the text first).
- **Follow up.** Use Instagram's reply on one of the bot's answers, or just write a question: the agent finds the earlier post in the records.
- **The archive.** Every post gets a Markdown record in `data/records/`: summary, speech, on-screen text, a `Names` line (people, accounts, tools, libraries, repos, sites, songs) and what you asked with the bot's answer. The raw material (video, frames, full transcript, Instagram's metadata) stays in `data/cache/`. Point any agent or a plain search at `data/records/` to find a post again.

## Configuration

All settings live in `config.toml` (see `config.example.toml` for comments):

| Setting | Meaning |
|---|---|
| `instagram.owner` | the only account whose messages are answered |
| `instagram.browser` | `chrome`, `msedge` or `chromium` |
| `schedule.interval_minutes` | how often to check (default 5) |
| `schedule.max_workers` | turns answered at once (default 2) |
| `agent.kind` | `claude`, `codex` or `custom` |
| `agent.model` | optional model name for the agent |
| `agent.command` | for `custom`: your command, `{prompt_file}` is replaced with the prompt's path |
| `reply.language`, `reply.max_chars`, `reply.rules` | how the replies read |
| `gemini.api_keys` | one or more keys; they rotate when one runs out of its free daily quota |
| `paths.data_dir`, `paths.user_dir` | where the archive and the browser profile live |

The agent's instructions are in `prompts/answer.md`; edit them to change the bot's manner.

## Commands

```
python botctl.py login | status | cookies
python botctl.py check [--now | --dry-run]
python botctl.py run
python botctl.py schedule install | remove
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

- **Replies stopped.** Look at `data/logs/watch.log`. "logged out" means Instagram ended the bot's session: run `python botctl.py login` again.
- **A turn keeps failing.** Each agent run writes `data/logs/agent_*.log`. A turn is tried twice, then left alone until you send something new.
- **Windows:** a headless browser started by Task Scheduler cannot decrypt its own saved cookies, so the bot loads its session from the cookies file it keeps in the app-data folder. Nothing to do; this is only why that file exists.

## Status

Built and used daily on Windows 11 with Claude Code and Microsoft Edge. The macOS (launchd) and Linux (systemd) schedulers and the Codex agent option are written but not yet tested; reports and fixes are welcome.

## Credits

The reel pipeline comes from [claude-reel](https://github.com/Murtadha-Najem/claude-reel). Third-party components and models are listed in [THIRD_PARTY.md](THIRD_PARTY.md). MIT licence.

---

## بالعربي

بوت يتركب على حساب انستا ثانوي: ترسله ريل أو بوست أو سؤال من حسابك، وخلال دقايق يرد عليك بنفس المحادثة كرد على رسالتك. يفهم الفيديو (الكلام، الكتابة على الشاشة، الأغنية، اللقطات) ويجاوب على سؤالك بالضبط، ويحفظ سجل لكل شي شافه على جهازك حتى تسأله عنه بعدين بدون ما يفتح انستا.

- يشتغل على جهازك، والفهم والكتابة يسويها وكيل برمجي انت مسجل بي: Claude Code أو Codex.
- الفحص كل كم دقيقة ما يصرف شي، والوكيل ما يشتغل إلا من توصل رسالة جديدة.
- أي نص ترسله خلال 3 دقايق بعد الريل يعتبر سؤال عنه.
- يرد بس على حسابك انت، ويتجاهل أي حساب ثاني.
- استعمل حساب ثانوي للبوت، مو حسابك الأساسي: أتمتة الحساب مخالفة لشروط انستا وممكن ينحظر.

التثبيت والإعدادات بالأعلى. اللغة تنضبط من `config.toml`، وأكو مثال لقواعد الكتابة بالعربي داخل `config.example.toml`.
