# Project skills

Skills here load into every worker that runs in this repo (Claude Code reads `.claude/skills`, Codex reads
`.agents/skills`, which is a symlink to this directory). Keep the set small: every skill's description sits
in each run's context.

Vendored, unmodified:

- `test-driven-development`, `verification-before-completion`, `systematic-debugging` from
  [obra/superpowers](https://github.com/obra/superpowers) (MIT, Jesse Vincent). Only these three are used;
  the full plugin's session hook and interview-style skills are meant for a human at the keyboard.
- `huggingface-local-models`, `hf-mem`, `huggingface-best`, `transformers-js` from
  [huggingface/skills](https://github.com/huggingface/skills) (Apache-2.0). The full plugin has 25 skills and
  costs ~5k tokens per run; these four cover model choice, memory budgeting, local inference and in-browser ML.

Add your own: one directory per skill with a `SKILL.md` (`name:` and `description:` front matter). The
orchestrator should write a `<stack>` skill on day one pinning the chosen libraries, versions, sample rates and
file layout, so every worker builds on the same decisions.
