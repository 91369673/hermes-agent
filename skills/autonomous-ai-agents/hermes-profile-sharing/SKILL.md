---
name: hermes-profile-sharing
description: "Share Hermes profiles across humans or across a multi-profile installation while preserving persona boundaries, access control, and auditable ownership."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, profiles, gateway, telegram, sharing, access-control, skills, memory]
    related_skills: [hermes-agent, hermes-profile-migration, hermes-telegram-profiles]
---

# Hermes Profile Sharing

Use this skill when the user wants to expose an existing Hermes profile to another chat/user, or when a multi-profile installation should share capabilities and operational knowledge without erasing each profile's persona.

This complements `hermes-agent`. Load `hermes-agent` first for canonical CLI syntax, then use this skill for the profile-sharing safety model and verification steps.

## Core models

There are two different sharing problems:

1. **Access sharing:** one existing profile should be reachable from another Telegram group, thread, or human.
2. **Knowledge/capability sharing:** several profiles should see the same skills and installation-level operating principles while retaining distinct identities.

Do not conflate these with cloning or merging profiles. Profiles can share surfaces and skills while keeping separate `SOUL.md`, memory files, config, sessions, cron stores, and gateway state.

## Access sharing: Telegram group pattern

Use when the user wants another person or group to interact with an existing Telegram-backed profile.

1. Create a dedicated Telegram group or forum topic for the shared context.
2. Add the profile's Telegram bot to that group.
3. Ask the user to send one explicit mention in the group, e.g. `@botname hi`.
4. Discover the Telegram chat ID from the profile gateway logs or channel directory.
5. Whitelist only that chat in the target profile config.
6. Require explicit mentions by default.
7. Restart only the affected profile gateway.
8. Test both a mentioned message and an unmentioned message.

Recommended config shape:

```yaml
telegram:
  allowed_chats:
    - "-1001234567890"
  require_mention: true
  reactions: false
  channel_prompts: {}
```

Restart:

```bash
hermes --profile <profile> gateway restart
```

Optional user allowlist once user IDs are known:

```yaml
telegram:
  allowed_chats:
    - "-1001234567890"
  require_mention: true
  group_allow_from:
    - "<trusted-user-id>"
```

## Knowledge/capability sharing: superagent pattern

Use when the user explicitly wants all profiles in an installation to share skills and operational knowledge while keeping different personas/roles.

Preferred implementation:

1. **Do not symlink or hard-merge persona files.** Keep each profile's own:
   - `SOUL.md`
   - `memories/MEMORY.md`
   - `memories/USER.md`

2. **Share skills with `skills.external_dirs`.** For every profile, set `skills.external_dirs` to the `skills/` directories of the other profiles plus the default profile's `~/.hermes/skills` when applicable.

3. **Add one compact shared operating-principle block to each profile's own `SOUL.md`.** State that profiles should share capabilities and operational knowledge, but preserve persona, role, and auditable action ownership.

4. **Back up before editing.** Save every touched `config.yaml` and `SOUL.md` before modification.

5. **Validate and restart.** Run `config check` for every profile and restart affected gateways so cached agents rebuild their system prompts and skill indexes.

Example inspection/update skeleton:

```bash
LOGIN_HOME="$HOME"  # use the real login home; profile gateways may override HOME
python - <<'PY'
from pathlib import Path
import shutil, yaml

home = Path.home()
root = home / ".hermes"
profiles = [root] + [p for p in sorted((root / "profiles").iterdir()) if p.is_dir() and (p / "config.yaml").exists()]
skill_dirs = [str(p / "skills") for p in profiles if (p / "skills").is_dir()]
backup = root / "backups" / "profile-sharing"
backup.mkdir(parents=True, exist_ok=True)

shared_block = """## Shared Hermes installation knowledge

This installation expects profiles to share capabilities, skills, and operational knowledge while preserving distinct personas, roles, and explicit ownership of side effects.
""".strip()

for profile_root in profiles:
    name = "default" if profile_root == root else profile_root.name
    for filename in ["config.yaml", "SOUL.md"]:
        src = profile_root / filename
        if src.exists():
            shutil.copy2(src, backup / f"{name}.{filename}.bak")

    cfg_path = profile_root / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text()) or {}
    skills_cfg = data.setdefault("skills", {})
    local_skills = str(profile_root / "skills")
    skills_cfg["external_dirs"] = [p for p in skill_dirs if p != local_skills]
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    soul_path = profile_root / "SOUL.md"
    if soul_path.exists():
        text = soul_path.read_text()
        if "## Shared Hermes installation knowledge" not in text:
            soul_path.write_text(text.rstrip() + "\n\n" + shared_block + "\n")
PY
```

Then validate:

```bash
hermes config check
for profile in $(hermes profile list --plain 2>/dev/null || true); do
  hermes --profile "$profile" config check
  hermes --profile "$profile" gateway restart
 done
hermes profile list
```

Adapt the profile listing command to the currently available CLI output; do not rely on a non-existent `--plain` flag without checking.

## Shared memory caution

Literal memory merging is usually the wrong first step.

Avoid:

- concatenating every profile's `MEMORY.md` into every other profile;
- symlinking all `USER.md` files to one shared file;
- overwriting persona-specific memories with another profile's notes.

Prefer:

- shared skills for reusable procedures;
- shared reference files or a shared knowledge base for installation-wide facts;
- a dedicated operations/chronicle profile for cron responses, watchdogs, and operational history;
- explicit handoff messages between profiles when a human wants one profile to take over another profile's alert.

## Safety defaults

- Start with narrow sharing; widen only after validation.
- Keep DMs separate from group access unless explicitly intended.
- Require mention in groups by default.
- Do not expose broad terminal/file tools to additional humans until access control is tested.
- Shared knowledge does not mean uncontrolled side effects. Keep owner, profile, job ID, workdir, and delivery target explicit.
- Restart only affected gateways unless the change intentionally touches every profile.
- Never print Telegram bot tokens or other secrets while reporting verification.

## Verification checklist

For access sharing:

- [ ] Correct bot is in the Telegram group.
- [ ] Correct chat ID is whitelisted in the correct profile only.
- [ ] Gateway restart succeeded for that profile.
- [ ] Mentioned message triggers a response.
- [ ] Unmentioned message does not trigger a response when `require_mention: true`.
- [ ] Any user allowlist behaves as intended.

For knowledge/capability sharing:

- [ ] Every touched file was backed up.
- [ ] Each profile's `skills.external_dirs` points only to existing directories.
- [ ] Each profile still has its own `SOUL.md`, `MEMORY.md`, and `USER.md`.
- [ ] Shared operating-principle block is present where intended.
- [ ] `hermes config check` passes for default and every named profile.
- [ ] Gateways restart and `hermes profile list` shows expected running profiles.
