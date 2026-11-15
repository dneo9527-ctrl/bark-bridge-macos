---
name: codex-bark-wait
description: Send this Mac user's shared Bark reminder with chat name and a short question/choice preview when Codex waits for their answer. Use before asking for input in a local chat.
---

Send one reminder before asking a question or presenting choices that require a reply. The user requested the chat name, minimal question and choice labels on their existing Bark phone.

For a content preview, run this fixed command with an interactive stdin (`tty: true` when using `exec_command`):

```sh
@BARK_PYTHON@ "@BARK_BASE@/bark.py" wait --preview-json
```

Once its session is waiting for input, use `write_stdin` to send one compact JSON line followed by a newline. The command disables terminal echo. For example, the data shape is:

```json
{"questions":[{"question":"Which option should we use?","options":["Keep current settings","Apply the proposed change"]}]}
```

Prepare the actual minimal question and choice labels before invoking the command. Do not send a generic reminder first and then a preview: the 15-second shared deduplication window keeps the first reminder. Supported question-tool hooks are a second path and are coalesced with this Skill. Match the preview to what you actually ask; do not invent choices. Optionally supply `chat_title` only when the current chat's display name is already known. Otherwise the sender resolves its name locally from `CODEX_THREAD_ID`.

Keep preview content out of shell command strings, process arguments and temporary files. Pass it through stdin; JSON newlines inside strings must be escaped. Include at most two short questions and five choice labels each. Do not include full commands, files, transcripts, credentials, keys, passwords, personal records or other sensitive details. Summarize sensitive requests generically. Common credential patterns are masked as a safeguard, not a guarantee.

If interactive stdin is unavailable, or there is no suitable non-sensitive preview, use the backward-compatible fallback:

```sh
@BARK_PYTHON@ "@BARK_BASE@/bark.py" wait
```

The user can turn off names and content through Bark > 来源 > Codex > … > 手机内容预览. In that mode the sender uses the original generic reminder. Phone delivery failure must never delay the question or change permissions. Do not read the Bark config, account credentials or transcript files. Status is available via `bark.py status` without secrets.

Use for plain questions, choices and intentional waits for user input, once per question group. Do not send for rhetorical questions, progress updates, optional closing offers or completion. Permission requests use their existing Hook; never auto-approve or alter an approval decision.

Coverage remains best effort: older chats with cached instructions may still send a generic question; specialized tools, cloud orchestration, sleeping/offline Macs and APNs failures may miss delivery. The iPhone displays previews only; answers and approvals still happen on the Mac.
