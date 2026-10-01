# confkit — specification

Build `confkit`, a pure-Python (3.11+, standard library only) package for a small
configuration language, in the directory `confkit/` at the root of this repository, with a
command-line interface runnable as `python -m confkit`. Behaviour not specified here is up to
you, but everything specified is checked by an acceptance test suite you do not see. Write
your own tests under `tests/`.

## 1. Public API (`import confkit`)

| Name | Description |
|---|---|
| `loads(*texts, env=None, prefix="CONFKIT_", schema=None) -> Config` | Parse one or more texts as layers (later overrides earlier, §4), apply environment overrides (§5) if `env` is a mapping, interpolate (§3), then validate against `schema` (§6) if given. |
| `load(*paths, env=None, prefix="CONFKIT_", schema=None) -> Config` | Same, reading UTF-8 files (`str` or `pathlib.Path`). Errors report the file path as `source`. |
| `Config` | The result (§7). |
| `ConfkitError` | Base class of all errors below. |
| `ParseError` | Syntax errors; attributes `line` (1-based int) and `source` (`"<string>"` for texts, or the path as given, converted with `str()`). |
| `InterpolationError`, `MergeError`, `ValidationError` | See §3, §4, §6. `ValidationError.errors` is a list of strings. |

## 2. Syntax

A file is a sequence of lines (`\n` or `\r\n`).

- **Blank lines** and lines whose first non-blank character is `#` or `;` are ignored.
- **Section header**: `[name]`, surrounding whitespace allowed, where `name` matches
  `[A-Za-z0-9_.-]+`. Dots are ordinary characters: `[server.http]` is the section named
  `server.http`. A section header may appear more than once in a file; the keys accumulate.
- **Assignment**: `key = value` or `key += value`. `key` matches `[A-Za-z_][A-Za-z0-9_-]*`.
  Whitespace around the key, operator and value is ignored.
- A key assigned before any section header, a malformed header, a line that is neither of the
  above, or assigning the same key twice in the same section **within one text** (with `=`)
  raises `ParseError` with the line number of the offending line.
- **Continuation**: if a line (after removing an inline comment) ends with a backslash `\`,
  the backslash is removed and the next line is appended, joined by exactly one space, after
  stripping both parts. Continuations may chain. The error line of a continued assignment is
  its first line.
- **Inline comments**: outside quotes, a `#` or `;` that is preceded by whitespace starts a
  comment that runs to the end of the line.

### 2.1 Values

The value text (stripped) is typed as follows, in this order:

1. `"..."` — a double-quoted string. Escapes: `\n`, `\t`, `\"`, `\\`; any other backslash
   sequence is a `ParseError`. Nothing but an inline comment may follow the closing quote.
2. `'...'` — a single-quoted string, taken literally (no escapes, no interpolation).
3. `true` / `false` (case-insensitive) — bool.
4. Integer: optional `-`, digits, single underscores allowed between digits (`1_000`) — int.
5. Float: optional `-`, digits, `.`, digits, optional exponent `e`/`E` with optional sign and
   digits (`-1.5`, `2.0e-3`) — float. (`1.` and `.5` are not floats; they are strings.)
6. List: `[` ... `]` on one logical line; comma-separated items, each typed by rules 1–5
   (quoted items may contain commas and brackets); whitespace around items ignored; `[]` is
   the empty list; a trailing comma is allowed; nested lists are a `ParseError`.
7. Anything else — an unquoted string (possibly empty).

## 3. Interpolation

`${name}` inside an unquoted string, a double-quoted string, or a string item of a list is
replaced after all layers are merged and environment overrides are applied.

- `${key}` refers to a key of the same section; `${sec.key}` refers to `key` in section
  `sec`, where the split is at the **last** dot (`${a.b.c}` is key `c` of section `a.b`).
- If the whole value is exactly one reference (e.g. `port = ${base.port}`), the result has the
  referenced value's type (int, list, …). Otherwise each referenced value is converted to text
  (bools as `true`/`false`, lists as `[a, b]` using the same text form as §7 `dumps`) and
  concatenated.
- `$$` produces a literal `$`. A `$` not followed by `{` or `$` is literal.
- A reference to a missing section or key, or a cycle, raises `InterpolationError` naming the
  reference.
- Values from single-quoted strings are never interpolated, and neither are values produced by
  interpolation (the result of `${x}` is not scanned again).

## 4. Layers and merging

Layers are applied in order. For each section and key, a later `=` replaces the earlier value
(of any type). `key += value` appends: the value must be a list or a scalar (a scalar is
treated as a one-item list); the earlier value must be a list or absent (absent = empty list).
If the earlier value exists and is not a list, raise `MergeError`. Within a single text, `+=`
may follow an `=` of the same key (the append applies to it) and may repeat.

## 5. Environment overrides

With `env` a mapping (e.g. `os.environ`), every variable whose name starts with `prefix`
overrides one key: the rest of the name is split at the first `__` (double underscore) into
section and key, both lower-cased. A section is matched by comparing with the existing section
names lower-cased and with `.` replaced by `_`; if none matches, a new section with the
lower-cased name is created. The value is typed by §2.1 (quotes and lists work; no
continuation, no comments). Names without `__` or with an empty section or key are ignored.
Overrides are applied after merging and before interpolation.

## 6. Schema validation

A schema is a dict `{section: {key: spec}}`; a spec is a dict with optional fields:

| field | meaning |
|---|---|
| `type` | one of `"str"`, `"int"`, `"float"`, `"bool"`, `"list"`; an int is accepted for `"float"` and converted to float; bool is **not** accepted as int |
| `required` | bool, default false |
| `default` | used when the key is missing (the section is created if needed); a key with a default is never "missing" |
| `min`, `max` | inclusive bounds for int/float values, and for the length of str and list values |
| `choices` | list of allowed values |
| `items` | element type for `"list"` (same names as `type`; int accepted for float items and converted) |

A section spec may contain `"__strict__": true`, in which case keys present in the config but
absent from the schema are errors. Sections not in the schema are not checked.

Validation collects **all** problems and raises one `ValidationError` whose `errors` is the
sorted list of messages, each starting with `"<section>.<key>: "`. Otherwise it returns the
config with defaults filled in and int→float conversions applied.

## 7. `Config`

- `config.sections()` — list of section names, in first-appearance order across layers
  (sections created by env or defaults come after).
- `config[section]` — a `dict` copy of that section's keys; `KeyError` if absent.
  `section in config` works.
- `config.get("sec.key", default=None)` — split at the last dot; returns `default` if the
  section or key is missing.
- `config.to_dict()` — `{section: {key: value}}` (plain dicts, deep copy).
- `config == other` compares `to_dict()`.
- `config.dumps()` — text in this format that `loads` parses back to an equal `Config`:
  sections in order, one `key = value` per line; strings always double-quoted with escapes and
  with `$` written as `$$`; bools `true`/`false`; floats via `repr`; lists as `[a, b]` with
  items in the same forms; a blank line between sections.

## 8. Command line

`python -m confkit <command> ...`; environment overrides with prefix `CONFKIT_` from the
process environment always apply.

- `get FILE [FILE ...] KEY` — prints the value of `KEY` (`sec.key`): strings raw, bools
  `true`/`false`, numbers as Python prints them, lists as JSON. Missing key: message on
  stderr, exit code 2.
- `dump FILE [FILE ...] [--json]` — prints `Config.dumps()`, or with `--json` the
  `to_dict()` as JSON with sorted keys and 2-space indentation.
- `validate FILE [FILE ...] --schema SCHEMA.json` — prints `ok` and exits 0, or prints each
  error on its own line and exits 1.
- Any `ConfkitError` other than validation: print `error: ` followed by a message on stderr
  and exit 1; for `ParseError` the message is `<source>:<line>: <detail>`.

## 9. Deliverables

- the `confkit/` package with the API and CLI above;
- your tests under `tests/` (pytest), all passing;
- a short `README.md` with usage examples.
