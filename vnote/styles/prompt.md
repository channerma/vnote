---
description: a Claude Code session prompt — paste it as the opening message
output: plain
backend: claude-code
---
Turn this spoken brief into the opening prompt for a coding-agent session, written in the speaker's own voice and addressed to the agent. Keep every specific the speaker gave — file names, numbers, tool names, examples, the order they want things done — and do not add scope, options or advice they did not voice. Remove filler, false starts and repetition; resolve spoken corrections ("actually, scratch that") by keeping only the final intent.

Organize it as short sections, each present only when the speaker gave material for it:
- **What I want** — the task, stated once, concretely.
- **Context** — what the agent needs to know about the project, the current state, and why this matters.
- **Constraints and preferences** — anything the speaker said to do, avoid, or prefer, including style, tools, and process.
- **Done when** — how the speaker will know it worked, if they said so; otherwise omit the section rather than inventing a check.
- **Open questions** — things the speaker was unsure about, phrased as questions for the agent to ask before acting.

Plain GitHub-flavored Markdown; no preamble, no closing remarks, nothing addressed to anyone but the agent.
