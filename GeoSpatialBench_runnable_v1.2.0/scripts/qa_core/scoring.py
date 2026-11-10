#!/usr/bin/env python3
"""Evaluate paired open/MCQ predictions for the 700-item L1 benchmark."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path


LABELS = ("equals","contains","within","covers","overlaps","crosses","touches","intersects","disjoint")


def read_jsonl(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def norm(x):
    return re.sub(r"\s+", " ", str(x).strip().lower()).replace("→", ">")


NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
LEGACY_SCORING_VERSION = "final_answer_v2_20260910"
SCORING_VERSION = "complete_semantic_final_answer_v3_20261014"


def _answer_lines(raw):
    text = str(raw).strip().replace("−", "-")
    return [s.strip() for s in text.splitlines()
            if s.strip() and s.strip() not in ("\\]", "\\)", "```", "$$", "---")]


def extract_final_number(raw):
    """Gold-independent extraction. Never fall back to a step/coordinate number."""
    text = str(raw).strip().replace("−", "-")
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    boxes = list(re.finditer(r"\\boxed\s*\{\s*(" + NUMBER_PATTERN +
                            r")\s*(?:\^?\\circ)?\s*\}", text))
    if boxes and not re.search(NUMBER_PATTERN, text[boxes[-1].end():]):
        value = float(boxes[-1].group(1))
        return (value, "terminal_box") if math.isfinite(value) else (None, "nonfinite")
    lines = _answer_lines(text)
    if not lines:
        return None, "no_final_answer"
    if len(lines) > 1 and re.fullmatch(r"\d+\.", lines[-1]):
        return None, "unfinished_numbered_list"
    tail = lines[-1].replace("**", "").replace("`", "").replace("\\(", "").replace("\\)", "").strip("$ ")
    tail = re.sub(r"^(?:final\s+answer|answer|result)\s*[:=]\s*", "", tail, flags=re.I)
    match = re.fullmatch("(" + NUMBER_PATTERN +
                         r")\s*(?:m|meters|metres|degrees|°|\\circ|\^\\circ|POIs/km²|POIs/km\^2)?\s*\.?",
                         tail, re.I)
    if not match:
        return None, "no_unambiguous_final_number"
    value = float(match.group(1))
    return (value, "terminal_number") if math.isfinite(value) else (None, "nonfinite")


def extract_final_letter(raw):
    """Only an explicit terminal choice; prose articles and option lists are not answers."""
    lines = _answer_lines(raw)
    if not lines:
        return None, "no_final_answer"
    clean = lambda line: line.replace("**", "").replace("`", "").strip()
    tail = clean(lines[-1])
    prefix = r"(?:(?:so\s+)?(?:the\s+)?(?:correct\s+)?(?:final\s+)?(?:answer|choice|option)|final)"
    explicit = re.match(prefix + r"(?:\s+is)?\s*[:=]?\s*", tail, re.I)
    if explicit:
        tail = tail[explicit.end():].strip()
    tail = re.sub(r"^\\boxed\{([A-D])\}$", r"\1", tail, flags=re.I)
    match = re.fullmatch(r"([A-D])[.)]?", tail, re.I)
    if match:
        return match.group(1).upper(), "terminal_letter"
    # A terminal labelled choice is also an answer, unless it is merely the
    # last member of an option list or contains another competing choice.
    match = re.fullmatch(r"([A-D])[.)]\s*(\S.*)", tail, re.I)
    if match:
        suffix = match.group(2)
        competing = re.search(r"\b[A-D]\b", suffix)
        previous_option = len(lines) > 1 and re.match(r"^[A-D][.)]\s*\S", clean(lines[-2]), re.I)
        if not competing and (explicit or not previous_option):
            return match.group(1).upper(), "terminal_labelled_choice"
    return None, "no_unambiguous_final_letter"


def extraction_audit(pred, gold, form, legacy=False):
    """Keep answer correctness distinct from the instruction to output only an answer."""
    if form == "mcq4":
        value, rule = extract_final_letter(pred)
        compliant = str(pred).strip() in {"A", "B", "C", "D"}
    elif isinstance(gold.get("answer"), (int, float)):
        value, rule = extract_final_number(pred)
        compliant = bool(re.fullmatch(NUMBER_PATTERN, str(pred).strip()))
    else:
        return {"scoring_version": LEGACY_SCORING_VERSION if legacy else SCORING_VERSION,
                "extraction_rule": "legacy_non_numeric" if legacy else "complete_terminal_answer",
                "format_compliant": None}
    return {"scoring_version": SCORING_VERSION, "extraction_rule": rule,
            "answer_extracted": value is not None, "format_compliant": compliant}



def final_answer_text(raw):
    """Extract a complete terminal answer without searching for a gold token."""
    lines = _answer_lines(raw)
    if not lines:
        return ""
    text = lines[-1].replace("**", "").strip("` \"'")
    text = re.sub(r"^(?:final\s+answer|answer|result)\s*(?::|=|\bis\b)\s*", "", text, flags=re.I)
    return text.rstrip(".。!！ ").strip()


def score_open(pred, gold, legacy=False):
    expected, scoring = gold["answer"], gold.get("scoring", {})
    typ, p, e = scoring.get("type", "exact"), norm(pred), norm(expected)
    if e in LABELS and legacy:
        found = [x for x in LABELS if re.search(rf"\b{x}\b", p)]
        return bool(found and found[0] == e), found[0] if found else None
    if isinstance(expected, (int, float)) or typ in {"numeric", "circular_numeric"}:
        value, _ = extract_final_number(pred)
        if value is None: return False, None
        if typ == "circular_numeric":
            error = abs((value-float(expected)+180) % 360-180)
            tolerance = float(scoring.get("absolute_tolerance_deg", 1))
        else:
            error = abs(value-float(expected))
            tolerance = float(scoring.get("absolute_tolerance_m", scoring.get("absolute_tolerance", 0)))
        return error <= tolerance, value
    if not legacy:
        p = norm(final_answer_text(pred))
    if p == e: return True, p
    short_tokens = {"yes","no","n","e","s","w","ne","se","sw","nw","front","right","back","left","same_side","opposite_sides","same_layer"}
    if legacy and e in short_tokens:
        return bool(re.search(rf"(?<![a-z_]){re.escape(e)}(?![a-z_])", p)), p
    compact = lambda x: re.sub(r"\s+", "", x)
    return compact(p) == compact(e), p


def score_mcq(pred, gold):
    letter, _ = extract_final_letter(pred)
    return letter == gold["answer_letter"], letter

