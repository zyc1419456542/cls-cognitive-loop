# Glossary — Internal Terms → Public Terms

The v3.1 full release sanitizes personal identifiers. Code comments preserve the
internal vocabulary you'll see in annotation timestamps; this table maps them.

| In code (sanitized) | Originally | Meaning |
|--------------------|-----------|---------|
| `incident-log` | 暴毙录 ("sudden-death log") | append-only incident register; every ≥5-min debugging failure gets an entry via `baobi_recorder.py` |
| `baobi` | pinyin of 暴毙 | kept in file identifiers (renaming would break import chains); content uses "incident" |
| `deliveries/` | 许墨交付/ | output directory for every task deliverable |
| `knowledge/` | 知识库/ | knowledge base (KG cards, conclusion library, iteration records) |
| `persona/` | 灵魂/ | persona definition directory — never enters the cognitive loop, never in git |
| `maintainer` | 翼辰 / 张翼辰 | the human operator |
| `assistant` | 许墨 | the assistant persona name |
| `dual-track` | 双轨 | one write lands twice: human-readable narrative + machine-parsable YAML frontmatter |
| `consult` | 会诊 | tier-3 external-model review on the difficulty ladder |
| `stance / gear` | 档位 | farming/skirmish/teamfight/retreat operational modes |
| `CD check` | CD清点 (game slang: "cooldown check") | pre-engagement inventory of unreviewed debts |
| `<REPO_ROOT>` | absolute path placeholder | replace at deploy; most code derives paths from `__file__` anyway |
| `<DSH_HOME>` | dsh config home | DeepSeek Harness `$DSH_HOME` |

## Deployment placeholders

`grep -rn "<REPO_ROOT>\|<DSH_HOME>\|<HOME>"` after cloning — every hit is a site
you may need to configure (most are fallback literals; env vars take precedence).
API keys are read from environment / `keys/` (gitignored) — never hardcoded.

## Domain redaction (2026-08-28)

The maintainer's employer and engineering domain are redacted repo-wide:

| Placeholder | Was |
|-------------|-----|
| `<ORG>` / `<ORG_REDACTED>` | employer name |
| `<DOMAIN>` / `<DOMAIN设备>` | the engineering field / its device |
| `<介质>` `<传感器>` `<流场>` | field-specific physical terms |
| `<部件A>` `<部件B>` / `<part-A>` `<part-B>` | two device components |
| `<sensor>` `<flow-field>` `<DOMAIN device>` | English equivalents |

Training data under `model-training/data/` uses the same placeholders, so the
data remains structurally intact (and mutually consistent with the extraction
scripts' patterns) while the domain itself stays unnamed.
