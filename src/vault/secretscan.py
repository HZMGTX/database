"""Deciding whether a file's contents are safe to index.

This exists because of a specific, real situation: the repositories this
database is meant to index include one whose ``.env`` is committed on purpose
-- a private repo whose host deploys from it -- containing a live API token.
Indexing that file would copy the token into a full-text search index, a
typed attribute projection, revision snapshots and every export, which is a
far worse place for it than a git object.

So the rule is structural rather than best-effort: a file that trips any
check here is indexed by **path and metadata only**, its contents are never
stored, and the withholding is recorded so ``vault doctor`` can list exactly
what was held back. The user is told, not silently protected.

False positives are the acceptable failure. Withholding a harmless config
file costs a search hit; indexing a live credential costs the credential.
"""

import math
import re
from pathlib import PurePath
from typing import List, NamedTuple, Optional

__all__ = ["Verdict", "scan", "scan_path"]

# Filenames that are secrets by convention, whatever is in them.
SECRET_NAMES = {
    ".env", ".envrc", ".netrc", ".pgpass", ".htpasswd", "credentials",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "secrets.yml", "secrets.yaml",
    "secring.gpg", "shadow", ".pypirc", ".npmrc", "terraform.tfvars",
}
SECRET_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".asc", ".gpg",
}
SECRET_GLOBS = (
    ".env.*", "*.env", "*credential*", "*secret*", "*_rsa", "*_ed25519",
    "*.kdbx", "*.keychain",
)

# Token shapes that are unambiguous enough to act on by themselves.
TOKEN_PATTERNS: List[tuple] = [
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
    ("Stripe key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{24,}\b")),
    ("Discord bot token", re.compile(r"\b[MNO][A-Za-z\d_-]{23,25}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("Basic auth in a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{6,}@")),
    ("Slack webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}")),
]

# An assignment whose name says "secret" and whose value looks like one.
ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    \b(
        [A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|
        PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|AUTH|CREDENTIAL)[A-Z0-9_]*
    )
    \s*[:=]\s*
    ['"]?
    ([^\s'"#;,]{12,})
    """)

# Values that name a placeholder rather than hold a secret.
PLACEHOLDER = re.compile(
    r"(?i)^(?:your|my|the|example|sample|dummy|fake|test|changeme|placeholder|"
    r"xxx+|\.\.\.|<.*>|\$\{.*\}|\{\{.*\}\}|%s|None|null|true|false|undefined)",)

_PLACEHOLDER_WORDS = re.compile(
    r"(?i)(example|sample|dummy|placeholder|changeme|your[-_]?|xxxx|redacted|"
    r"insert[-_]?here|replace[-_]?me|<[^>]+>|\$\{[^}]+\}|\{\{[^}]+\}\})")

# A filesystem path or a dotted identifier is not a credential, even when it
# is long and its characters are varied. SECRET_KEY_FILE=/etc/app/secret.key
# names where a secret lives; it is not the secret.
_LOOKS_LIKE_PATH = re.compile(
    r"^(?:~?/|\./|\.\./|[A-Za-z]:\\|\\\\)"          # /etc/..., ./x, C:\...
    r"|^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$"            # a/b/c
    r"|^[a-z][a-z0-9+.-]*://"                             # a URL with no credentials
    r"|^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"  # module.attr.name
)


class Verdict(NamedTuple):
    withhold: bool
    reason: str

    def __bool__(self) -> bool:
        return self.withhold


def _entropy(value: str) -> float:
    """Shannon entropy per character.

    A random 32-character token sits near 4-5 bits; an English word or a
    dotted path sits well below 3.5.
    """
    if not value:
        return 0.0
    counts = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def scan_path(path: "str | PurePath") -> Verdict:
    """Judge a file by its name alone, before reading a byte of it."""
    p = PurePath(path)
    name = p.name
    lowered = name.lower()

    if lowered in SECRET_NAMES:
        return Verdict(True, f"{name} holds credentials by convention")
    if p.suffix.lower() in SECRET_SUFFIXES:
        return Verdict(True, f"{p.suffix} files hold keys or certificates")
    for pattern in SECRET_GLOBS:
        if PurePath(lowered).match(pattern):
            # .env.example and friends are documentation, not credentials.
            if _PLACEHOLDER_WORDS.search(lowered) or lowered.endswith((".example", ".sample", ".template", ".dist")):
                continue
            return Verdict(True, f"{name} matches {pattern}")
    return Verdict(False, "")


def scan(text: str, *, path: "str | PurePath | None" = None,
         max_bytes: int = 2_000_000) -> Verdict:
    """Judge a file by its name and its contents.

    Only the first *max_bytes* are examined: a credential that appears
    nowhere in the first two megabytes of a file is not what this is for, and
    scanning a 50 MB bundle line by line is not worth the time.
    """
    if path is not None:
        verdict = scan_path(path)
        if verdict.withhold:
            return verdict

    if not text:
        return Verdict(False, "")
    sample = text[:max_bytes]

    for label, pattern in TOKEN_PATTERNS:
        match = pattern.search(sample)
        if match:
            found = match.group(0)
            if _PLACEHOLDER_WORDS.search(found):
                continue
            return Verdict(True, f"looks like a {label}")

    for match in ASSIGNMENT_RE.finditer(sample):
        name, value = match.group(1), match.group(2)
        if PLACEHOLDER.match(value) or _PLACEHOLDER_WORDS.search(value):
            continue
        if _LOOKS_LIKE_PATH.match(value):
            continue
        # A long, high-entropy value assigned to something called SECRET is
        # the single most common shape of a leaked credential.
        if len(value) >= 16 and _entropy(value) >= 3.2:
            return Verdict(True, f"{name} is assigned a high-entropy value")
        if len(value) >= 24 and _entropy(value) >= 2.8:
            return Verdict(True, f"{name} is assigned a long opaque value")

    return Verdict(False, "")
