"""Ask chosen recall questions through the judge's own ask() against a snapshot and print the answer excerpts (diagnosis only).

usage: python3 -I show_answers.py SNAPSHOT_ID q1,q2 RUNS [QUESTIONS_FILE]   (default file: the live questions.jsonl; costs a few cents)
"""

import json
import os
import sys
import tempfile

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "engine")
sys.path.insert(0, os.path.join(ENGINE, "recall"))
sys.path.insert(0, ENGINE)
import memory_recall_run as mrr  # noqa: E402
import memory_common as mc  # noqa: E402
import memory_egress as eg  # noqa: E402

OPS = mc.ops_dir_for(mc.DEFAULT_MEMORY_DIR, mc.DEFAULT_OPS_ROOT)
snapshot, ids, runs = sys.argv[1], sys.argv[2].split(","), int(sys.argv[3])
memory = os.path.join(OPS, "snapshots", snapshot, "memory")
eg.require_clear(memory)  # this diagnostic sends note text to the provider too, so it passes the same gate
qfile = sys.argv[4] if len(sys.argv) > 4 else os.path.join(OPS, "recall", "questions.jsonl")
questions = {q["id"]: q for q in mrr.load_questions(qfile)}
tmp = tempfile.mkdtemp(prefix="recall_diag_")
prompt = os.path.join(tmp, "prompt.txt")
index = open(os.path.join(memory, "MEMORY.md"), encoding="utf-8").read()
mrr.write_system_prompt(prompt, memory, mrr.visible_index(index))
cwd = os.path.join(tmp, "cwd")
os.makedirs(cwd, mode=0o700)
settings = mrr.Settings(mrr.DEFAULT_MODEL, 0.05)
spend = mrr.Spend(0.4)
for qid in ids:
    for n in range(runs):
        rec = mrr.ask(
            questions[qid], mrr.subprocess_runner, settings, prompt, memory, spend, cwd
        )
        print(
            "%s run %d pass=%s forbid_hit=%s"
            % (qid, n + 1, rec.get("pass"), rec.get("forbid_hit"))
        )
        print(
            "   ",
            json.dumps(rec.get("excerpt", rec.get("error")), ensure_ascii=False)[:600],
        )
print("spent: %.4f" % spend.total)
