#!/usr/bin/env python3
"""Build paired complete L1 open-response and substantive MCQ4 prompt packs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOPOLOGY_ROOT = Path("CONFIG_REQUIRED_USE_build_city_case.py")
MODES = ["wkt_exact", "geojson_centroid_bbox", "geojson_exact", "geojson_exact_attributes"]
LETTERS = "ABCD"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in rows), encoding="utf-8")


def stable_rng(key: str) -> random.Random:
    seed = int(hashlib.sha256(key.encode()).hexdigest()[:16], 16)
    return random.Random(seed)


def place_options(correct: str, distractors: list[str], correct_slot: int, key: str) -> tuple[list[str], str]:
    if correct in distractors:
        distractors = [x for x in distractors if x != correct]
    unique = []
    for x in distractors:
        if x not in unique and x != correct:
            unique.append(x)
    if len(unique) < 3:
        raise ValueError(f"Need three unique distractors for {key}: {unique}")
    stable_rng(key).shuffle(unique)
    options = unique[:3]
    options.insert(correct_slot, correct)
    return options, LETTERS[correct_slot]


def fmt_num(x: float, decimals: int = 2) -> str:
    if decimals == 0:
        return str(int(round(x)))
    s = f"{x:.{decimals}f}"
    return s.rstrip("0").rstrip(".")


def nice_step(value: float, relation_id: str) -> float:
    if relation_id == "M18":
        raw = max(value * .25, .5)
    else:
        raw = max(value * .20, 5.0)
    power = 10 ** math.floor(math.log10(raw))
    scaled = raw / power
    nice = 1 if scaled <= 1 else 2 if scaled <= 2 else 5 if scaled <= 5 else 10
    return nice * power


def numeric_intervals(value: float, relation_id: str, target_bin: int) -> tuple[str, list[str]]:
    step = nice_step(value, relation_id)
    start = max(0.0, value - (target_bin + .5) * step)
    bounds = [start + i * step for i in range(5)]
    unit = "POIs/km²" if relation_id == "M18" else "m"
    decimals = 2 if relation_id == "M18" else 0
    opts = [f"{fmt_num(bounds[i], decimals)} ≤ value < {fmt_num(bounds[i+1], decimals)} {unit}" for i in range(4)]
    actual_bin = next(i for i in range(4) if bounds[i] <= value < bounds[i+1])
    correct = opts[actual_bin]
    return correct, [x for x in opts if x != correct]


def non_topology_mcq(item: dict, item_index: int) -> tuple[str, list[str], str, dict]:
    rid = item["relation_id"]
    answer = item["gold"]["answer"]
    oracle = item["gold"]["oracle"]
    task = item["task"]
    if rid == "D01":
        correct, pool = answer, ["N", "E", "S", "W"]
    elif rid == "D02":
        pool = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        correct = answer
    elif rid == "D04":
        correct = f"{fmt_num(float(answer), 2)}°"
        v = float(answer)
        pool = [correct] + [f"{fmt_num((v+d)%360, 2)}°" for d in (15, 90, 180)]
    elif rid == "D05":
        correct, pool = answer, ["front", "right", "back", "left"]
    elif rid == "D06":
        correct, pool = answer, ["N-S", "NE-SW", "E-W", "NW-SE"]
    elif rid in {"M01", "M02", "M04", "M08", "M12", "M18"}:
        target_bin = item_index % 4
        correct, distractors = numeric_intervals(float(answer), rid, target_bin)
        options, letter = place_options(correct, distractors, target_bin, item["item_id"])
        task = task.replace("What is", "Which interval contains")
        return task, options, letter, {"mcq_semantics": "four mutually exclusive numeric intervals", "open_answer": answer}
    elif rid == "M14":
        d, r = oracle["distance_m"], oracle["buffer_radius_m"]
        pool = [
            f"distance ≤ 0.5r ({fmt_num(.5*r,1)} m)",
            f"0.5r < distance ≤ r ({fmt_num(r,1)} m)",
            f"r < distance ≤ 1.5r ({fmt_num(1.5*r,1)} m)",
            "distance > 1.5r",
        ]
        correct = pool[0] if d <= .5*r else pool[1] if d <= r else pool[2] if d <= 1.5*r else pool[3]
        task = f"Which distance band describes F02 relative to the closed {fmt_num(r,1)}-metre buffer radius r around F01?"
    elif rid == "M15":
        correct, pool = answer, ["F01", "F02", "F03", "F04"]
    elif rid == "O01":
        correct = answer
        a = correct.split(">")
        pool = [correct, ">".join(reversed(a)), ">".join([a[1], a[0], a[2]]), ">".join([a[0], a[2], a[1]])]
    elif rid == "O02":
        correct = answer
        reverse = "before=F02; after=F01" if "before=F01" in correct else "before=F01; after=F02"
        pool = [correct, reverse, "F01 and F02 have the same route position", "at least one point is not on F00"]
        task = "Which pairwise along-route statement is true for F01 and F02 in F00's stored coordinate direction?"
    elif rid == "O03":
        correct = "F01 and F02 are on the same side" if answer == "same_side" else "F01 and F02 are on opposite sides"
        pool = [correct,
                "F01 and F02 are on opposite sides" if answer == "same_side" else "F01 and F02 are on the same side",
                "F01 lies on F00 but F02 does not", "F02 lies on F00 but F01 does not"]
        task = "Which statement describes F01 and F02 relative to the directed straight line F00?"
    elif rid == "O04":
        if answer == "yes":
            correct = "F02 is strictly between F01 and F03"
        elif oracle["distance_to_supporting_line_m"] <= oracle["collinearity_tolerance_m"]:
            t = oracle["projection_parameter_unclamped"]
            correct = "F03 is strictly between F01 and F02" if t > 1 else "F01 is strictly between F02 and F03"
        else:
            correct = "the three points are not collinear"
        pool = ["F01 is strictly between F02 and F03", "F02 is strictly between F01 and F03", "F03 is strictly between F01 and F02", "the three points are not collinear"]
        task = "Which ordinary three-point collinearity/between statement is true?"
    elif rid == "O06":
        correct = "F01 is above F02" if answer == "F01" else "F02 is above F01" if answer == "F02" else "F01 and F02 are at the same layer"
        pool = ["F01 is above F02", "F02 is above F01", "F01 and F02 are at the same layer", "F01 and F02 do not intersect in 2D"]
    elif rid == "O08":
        steps = oracle["shortest_path_edges"]
        pool = ["reachable in 1–2 edges", "reachable in 3–4 edges", "reachable in 5 or more edges", "not reachable"]
        correct = "not reachable" if steps is None else pool[0] if steps <= 2 else pool[1] if steps <= 4 else pool[2]
        task = "Using only shared edge endpoints as nodes, which reachability statement from S to T is true? Interior crossings do not create nodes."
    else:
        raise KeyError(rid)
    distractors = [x for x in pool if x != correct]
    target_slot = item_index % 4
    options, letter = place_options(correct, distractors, target_slot, item["item_id"])
    return task, options, letter, {"mcq_semantics": "four mutually exclusive substantive outcomes", "open_answer": answer}


TOPO_CONFUSIONS = {
    "equals": ["overlaps", "contains", "disjoint", "intersects"],
    "contains": ["covers", "equals", "within", "overlaps"],
    "within": ["contains", "covers", "intersects", "disjoint"],
    "covers": ["contains", "touches", "intersects", "disjoint"],
    "overlaps": ["equals", "intersects", "touches", "disjoint"],
    "crosses": ["intersects", "touches", "overlaps", "disjoint"],
    "touches": ["intersects", "crosses", "disjoint", "overlaps"],
    "intersects": ["crosses", "touches", "overlaps", "disjoint"],
    "disjoint": ["intersects", "touches", "crosses", "within"],
}


def topology_mcq(pair: dict, item_index: int) -> tuple[list[str], str]:
    correct = pair["answer"]
    distractors = TOPO_CONFUSIONS[correct]
    return place_options(correct, distractors, item_index % 4, pair["pair_id"])


def options_block(options: list[str]) -> str:
    return "\n".join(f"{letter}. {text}" for letter, text in zip(LETTERS, options))


def topology_open_content(source_prompt: dict) -> str:
    return source_prompt["prompt"]


def topology_mcq_content(source_prompt: dict, pair: dict, options: list[str]) -> str:
    prompt = source_prompt["prompt"]
    marker = "Evidence representation:"
    evidence = prompt[prompt.index(marker):]
    return (
        "Select the most specific topological relationship from F01 to F02.\n"
        "Use this priority when more than one predicate is true:\n"
        "equals > contains > within > covers > overlaps > crosses > touches > intersects > disjoint.\n\n"
        "Which option is the most specific relationship?\n"
        f"{options_block(options)}\n\n{evidence}\n\nReturn one option letter only."
    )


def non_top_open_content(source_prompt: dict) -> str:
    return source_prompt["messages"][0]["content"]


def non_top_mcq_content(source_prompt: dict, task: str, options: list[str]) -> str:
    text = source_prompt["messages"][0]["content"]
    lines = text.splitlines()
    crs = lines[0]
    input_at = next(i for i, line in enumerate(lines) if line.startswith("Input ("))
    answer_at = next(i for i, line in enumerate(lines) if line.startswith("Answer format:"))
    evidence = "\n".join(lines[input_at:answer_at])
    return f"{crs}\nTask: {task}\n{evidence}\nOptions:\n{options_block(options)}\nReturn one option letter only."


def row(item_id: str, mode: str, form: str, content: str) -> dict:
    return {"prompt_id": f"{item_id}--{form}--{mode}", "item_id": item_id, "question_form": form, "representation_mode": mode, "messages": [{"role": "user", "content": content}]}


def build(root: Path, topology_root: Path) -> None:
    topology = read_jsonl(topology_root/"data"/"topology_v2_pairs_24.jsonl")
    non_top = read_jsonl(root/"data"/"non_topology_v1_items_57.jsonl")
    topo_prompts = {mode: {x["sample_id"]: x for x in read_jsonl(topology_root/"prompts"/f"topology_v2_{mode}_24.jsonl")} for mode in MODES}
    nt_prompts = {mode: {x["item_id"]: x for x in read_jsonl(root/"prompts"/f"non_topology_v1_{mode}_57.jsonl")} for mode in MODES}
    open_by_mode = {m: [] for m in MODES}; mcq_by_mode = {m: [] for m in MODES}
    open_gold=[]; mcq_gold=[]
    combined_index=0
    for pair in topology:
        options, letter = topology_mcq(pair, combined_index)
        open_gold.append({"item_id": pair["pair_id"], "source": "topology_v2", "answer": pair["answer"], "scoring": {"type": "exact_label"}})
        mcq_gold.append({"item_id": pair["pair_id"], "source": "topology_v2", "answer_letter": letter, "answer_option": options[LETTERS.index(letter)], "options": options})
        for mode in MODES:
            src=topo_prompts[mode][pair["pair_id"]]
            open_by_mode[mode].append(row(pair["pair_id"],mode,"open",topology_open_content(src)))
            mcq_by_mode[mode].append(row(pair["pair_id"],mode,"mcq4",topology_mcq_content(src,pair,options)))
        combined_index += 1
    for item in non_top:
        task, options, letter, semantics = non_topology_mcq(item, combined_index)
        open_gold.append({"item_id": item["item_id"], "source": "non_topology_v1", "answer": item["gold"]["answer"], "scoring": item["gold"]["scoring"]})
        mcq_gold.append({"item_id": item["item_id"], "source": "non_topology_v1", "relation_id": item["relation_id"], "answer_letter": letter, "answer_option": options[LETTERS.index(letter)], "options": options, **semantics})
        for mode in MODES:
            src=nt_prompts[mode][item["item_id"]]
            open_by_mode[mode].append(row(item["item_id"],mode,"open",non_top_open_content(src)))
            mcq_by_mode[mode].append(row(item["item_id"],mode,"mcq4",non_top_mcq_content(src,task,options)))
        combined_index += 1
    open_all=[]; mcq_all=[]
    for mode in MODES:
        assert len(open_by_mode[mode])==81 and len(mcq_by_mode[mode])==81
        write_jsonl(root/"prompts"/f"l1_v1_open_{mode}_81.jsonl",open_by_mode[mode])
        write_jsonl(root/"prompts"/f"l1_v1_mcq4_{mode}_81.jsonl",mcq_by_mode[mode])
        open_all.extend(open_by_mode[mode]); mcq_all.extend(mcq_by_mode[mode])
    write_jsonl(root/"prompts"/"l1_v1_open_all_modes_324.jsonl",open_all)
    write_jsonl(root/"prompts"/"l1_v1_mcq4_all_modes_324.jsonl",mcq_all)
    write_jsonl(root/"prompts"/"l1_v1_two_forms_all_648.jsonl",open_all+mcq_all)
    write_jsonl(root/"preds"/"l1_v1_open_private_gold_81.jsonl",open_gold)
    write_jsonl(root/"preds"/"l1_v1_mcq4_private_gold_81.jsonl",mcq_gold)
    report={"canonical_items":81,"topology_items":24,"non_topology_items":57,"question_forms":["open","mcq4"],"representation_modes":MODES,"prompts_per_form":324,"total_prompts":648,"mcq_correct_letter_counts":{x:sum(g["answer_letter"]==x for g in mcq_gold) for x in LETTERS}}
    (root/"reports"/"l1_v1_two_forms_build_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--root",type=Path,default=ROOT); ap.add_argument("--topology-root",type=Path,default=DEFAULT_TOPOLOGY_ROOT); args=ap.parse_args(); build(args.root,args.topology_root)


if __name__=="__main__": main()
