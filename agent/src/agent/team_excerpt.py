"""Extract an attributable decision field from a completed team's Markdown report."""

import re


_DECISION_HEADING = re.compile(
    r"(?:\d+(?:\.\d+)*[.、)]?\s*)?(?:最终(?:决策|裁决|结论|立场)|结论段|决策(?:声明|陈述)|"
    r"(?:final |team )?(?:decision|verdict|assessment))"
    r"(?:\s*[（(][^()（）\n]{1,32}[）)])?\s*", re.I,
)
_DECLARATION = re.compile(
    r"^(?:\*\*)?(?:我裁定|(?:团队|委员会|投委会)(?:裁定|决议|决定)|方向|最终(?:立场|裁决)|"
    r"(?:final |team )?(?:stance|decision|verdict|ruling))"
    r"(?:\*\*)?\s*[:：]", re.I,
)
_TABLE_DECISION = {"最终决定", "最终裁决", "团队结论", "pm 最终决定", "final decision", "team verdict"}
_TABLE_STANCE = {"方向", "立场", "最终立场", "stance", "direction"}


def _cells(line: str) -> list[str]:
    return [part.strip() for part in line.strip().strip("|").split("|")]


def _label(value: str) -> str:
    return value.strip("* ").casefold()


def team_decision_excerpt(report: str) -> str:
    """Return one *complete* explicitly labelled field. Never cut to fit.

    We prefer the author's final decision section over the executive summary
    table. Nested/ambiguous layouts, continued lines and corrections are left
    for the full report view. Extracting a sentence containing a stance word
    in arbitrary prose can reverse a conditional or quote the losing side.
    """
    lines = []
    fence = ""
    for line in report.splitlines():
        delimiter = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            if delimiter and delimiter[1].startswith(fence) and not line[delimiter.end():].strip():
                fence = ""
            lines.append("")
        elif delimiter:
            fence = delimiter[1]
            lines.append("")
        else:
            lines.append(line)
    headings = [index for index, line in enumerate(lines)
                if re.fullmatch(r"#{1,2}\s+.+", line)
                and _DECISION_HEADING.fullmatch(line.lstrip("#").strip())]
    if len(headings) > 1:
        return ""
    if headings:
        rest = lines[headings[0] + 1:]
        if any(re.match(r"^#{1,2}\s+", line) and not re.match(
                r"^#{1,2}\s+(?:附录|附[：:]|appendix\b|sources\b)", line, re.I,
        ) for line in rest):
            return ""
        first = next((index for index, line in enumerate(rest) if line.strip()), None)
        if first is None or not _DECLARATION.match(rest[first].strip()):
            return ""
        if first + 1 < len(rest) and rest[first + 1].strip():
            return ""
        candidate = rest[first].strip()
        # If the following paragraph explicitly qualifies/retracts the first
        # paragraph, showing the first in isolation would change its meaning.
        following = next((line.strip("* ") for line in rest[first + 1:] if line.strip()), "")
        if re.match(r"(?:但|不过|然而|仅当|只有|上述.{0,6}(?:不|仅)|更正|"
                    r"(?:but|however|only if|provided|subject to|correction)\b)", following, re.I):
            return ""
    else:
        choices = []
        for index, line in enumerate(lines[:-2]):
            header = _cells(line)
            if len(header) != 2 or _label(header[1]) not in _TABLE_DECISION:
                continue
            separators = _cells(lines[index + 1])
            if len(separators) != 2 or not all(re.fullmatch(r":?-{2,}:?", s) for s in separators):
                continue
            cells = _cells(lines[index + 2])
            # A multi-row table can put exclusions and conditions in other
            # rows. Only take an entire, terminal single-decision record.
            if (len(cells) == 2 and _label(cells[0]) in _TABLE_STANCE
                    and not any(row.strip() for row in lines[index + 3:])):
                choices.append(cells[1])
        if len(choices) != 1:
            return ""
        candidate = choices[0]
    # Allow emphasis but not links, tables, HTML, code, ellipses, or an
    # unbounded block. The caller independently checks claims and identity.
    if not candidate or len(candidate) > 800 or re.search(r"[<>\[\]`|…]|\.\.\.", candidate):
        return ""
    return candidate
