import argparse
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    # Gemini 3 deprecation notices about `temperature` on every call; aip still sets it deliberately.
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m regtech", description="RegTech Compliance Copilot")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="verify the environment, aip imports, corpus layout and manifest")
    p_check.add_argument("--live", action="store_true", help="also make one tiny chat + embed call")

    sub.add_parser("init-manifest", help="create data/manifest.csv with the header row if it does not exist")

    p_ingest = sub.add_parser("ingest", help="convert every manifest PDF to Markdown under data/processed/")
    p_ingest.add_argument("--only", help="convert a single doc_id")

    p_eval = sub.add_parser("eval-retrieval", help="Lab 3 retrieval sweep per corpus; writes reports/stage1_retrieval.*")
    p_eval.add_argument("--corpus", choices=["regulation", "internal_policy", "enforcement"],
                        help="run a single corpus (default: all three)")

    p_ask = sub.add_parser("ask", help="ask a question about the RBI Directions (grounded, cited)")
    p_ask.add_argument("question")
    p_ask.add_argument("--strict", action="store_true", help="refuse unless every part of the question is supported")

    p_qa = sub.add_parser("eval-qa", help="Lab 4 evaluation of the grounded Q&A; writes reports/stage2_qa.*")
    p_qa.add_argument("--no-judge", action="store_true", help="skip the LLM judges and the gold-context run")

    p_qt = sub.add_parser("qa-tier", help="Stage 7: Stage 2 quality + latency of the Q&A on a given model tier")
    p_qt.add_argument("tier")
    p_qt.add_argument("--label")

    sub.add_parser("judge-kappa", help="Cohen's kappa between the judge and your labels in the calibration sheet")

    p_gap = sub.add_parser("gap", help="check_policy_gap: compare a company's Fair Practices Code with the RBI rules on a topic")
    p_gap.add_argument("entity", help="NBFC name, e.g. 'IIFL'; or use --file")
    p_gap.add_argument("topic", help="e.g. 'gold loan auctions'")
    p_gap.add_argument("--file", help="path to an uploaded policy (.pdf/.md/.txt) instead of an indexed one")
    p_gap.add_argument("--json", action="store_true", help="print the structured GapReport as JSON")
    p_gap.add_argument("--precedents", action="store_true", help="also search RBI penalties for every gap found")

    sub.add_parser("build-cases", help="extract the structured enforcement case table (data/processed/enforcement_cases.json)")

    p_prec = sub.add_parser("precedent", help="find_enforcement_precedent: has the RBI penalised this kind of failure?")
    p_prec.add_argument("risk", help="the compliance risk in plain words")

    sub.add_parser("eval-precedent", help="Stage 4 evaluation; writes reports/stage4_precedent.{md,json}")

    sub.add_parser("eval-deadlines", help="Stage 5 Lab 2 comparison of extraction variants on the labelled dev set")

    p_aud = sub.add_parser("audit-calendar", help="write a seeded random sample of calendar entries for a precision audit")
    p_aud.add_argument("--n", type=int, default=20)

    p_bcal = sub.add_parser("build-calendar", help="extract every deadline in the regulation corpus into the calendar")
    p_bcal.add_argument("--prompt", default="B")
    p_bcal.add_argument("--tier", default="SMALL")

    p_up = sub.add_parser("upcoming", help="check_upcoming: what is due in the next N days")
    p_up.add_argument("days", type=int, nargs="?", default=30)
    p_up.add_argument("--as-of", help="YYYY-MM-DD (default: today)")
    p_up.add_argument("--doc", action="append", help="limit to a regulation doc_id (repeatable)")

    p_dl = sub.add_parser("deadlines", help="extract_deadlines from one document (a doc_id in the corpus)")
    p_dl.add_argument("doc_id")

    p_egap = sub.add_parser("eval-gap", help="Stage 3 evaluation of check_policy_gap; writes reports/stage3_policy_gap_<label>.*")
    p_egap.add_argument("--label", default="current")
    p_egap.add_argument("--tier", default="MAIN", help="model tier for requirement extraction")
    p_egap.add_argument("--assess-tier", default="SMALL", help="model tier for the per-requirement assessments")

    p_ag = sub.add_parser("agent", help="Stage 6: ask the compliance agent (it picks and calls the tools)")
    p_ag.add_argument("message")
    p_ag.add_argument("--layers", default="default", help="'default' (all but structured), 'all', 'none', or a '+'-joined subset of "
                      "delimit+detect+structured+privilege+output")
    p_ag.add_argument("--as-of", help="YYYY-MM-DD for calendar questions (default: today)")
    p_ag.add_argument("--confirm", action="store_true", help="ask you (y/n) before the privileged send tool runs")

    p_srv = sub.add_parser("serve", help="Stage 7: run the HTTP service (uvicorn) on localhost")
    p_srv.add_argument("--port", type=int, default=8000)

    sub.add_parser("semantic-study", help="Stage 7: find the semantic-cache similarity threshold that starts giving wrong answers")

    p_gate = sub.add_parser("gate", help="Stage 7 regression gate: golden sets vs ci/thresholds.yml (exit 1 on breach)")
    p_gate.add_argument("--record", action="store_true", help="also export the cache entries used to ci/cache/ for CI")
    p_gate.add_argument("--config", default=None)

    sub.add_parser("latency", help="Stage 7: end-to-end latency of the service by mode and stage (caches off)")

    sub.add_parser("make-fixtures", help="write the poisoned policy PDFs used by the red-team into tests/fixtures/redteam/")

    p_rt = sub.add_parser("redteam", help="Stage 6 red-team: attacks and controls under cumulative layers")
    p_rt.add_argument("--only", action="append", help="run only these case ids (repeatable)")
    p_rt.add_argument("--final-only", action="store_true", help="run only the all-layers configuration")
    p_rt.add_argument("--default-only", action="store_true", help="run only the default configuration (all but structured)")
    p_rt.add_argument("--label", default="stage6_redteam")
    p_rt.add_argument("--workers", type=int, default=6)

    args = parser.parse_args(argv)

    if args.command == "agent":
        from datetime import date as _date
        from regtech.agent import ALL_LAYERS, DEFAULT_LAYERS, ComplianceAgent, Layers, internal_only
        if args.layers in ("all", "default"):
            layers = ALL_LAYERS if args.layers == "all" else DEFAULT_LAYERS
        else:
            names = {"output": "output_filter"}
            on = [] if args.layers == "none" else args.layers.split("+")
            layers = Layers(**{names.get(n, n): True for n in on})
        confirm = internal_only
        if args.confirm:
            def confirm(tool: str, a: dict) -> bool:
                return input(f"\nAllow {tool} to {a.get('recipient')}? [y/N] ").strip().lower() == "y"
        agent = ComplianceAgent(layers, confirm_fn=confirm,
                                today=_date.fromisoformat(args.as_of) if args.as_of else None)
        run = agent.run(args.message)
        print(run.answer)
        print(f"\n[{run.layers}] stop={run.stop_reason} tools={[c['tool'] + ('' if c['ok'] else ' (denied)') for c in run.tool_calls]} "
              f"cost=${run.cost_usd:.4f} time={run.latency_ms / 1000:.1f}s")
        for f in run.flags:
            print("  flag:", f)
        for m in run.outbox:
            print(f"  outbox (simulated): to {m['recipient']}: {m['subject']}")
        return 0

    if args.command == "gate":
        from regtech.gate import CONFIG, main as gate
        return gate(Path(args.config) if args.config else CONFIG, record=args.record)

    if args.command == "latency":
        import json as _json
        from regtech.latency import run as lat
        out = lat()
        print(_json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "stages"} if isinstance(v, dict) else v
                           for k, v in out.items() if k != "service_metrics"}, indent=1))
        return 0

    if args.command == "serve":
        from regtech.service import main as serve
        serve(port=args.port)
        return 0

    if args.command == "semantic-study":
        from regtech.semantic_study import run as study
        o = study()
        print("lowest threshold with no wrong hit:", o["safe_threshold"], o["at_safe"])
        print("report: reports/stage7_semantic_threshold.md")
        return 0

    if args.command == "make-fixtures":
        from regtech.redteam import make_fixtures
        for p in make_fixtures():
            print("wrote", p)
        return 0

    if args.command == "redteam":
        from regtech.redteam import CASES, CONFIGS, run_suite, summarise, write_report
        cases = [c for c in CASES if not args.only or c.id in args.only]
        from regtech.agent import DEFAULT_LAYERS
        configs = CONFIGS[-1:] if args.final_only else [DEFAULT_LAYERS] if args.default_only else CONFIGS
        by_config = run_suite(configs, cases, workers=args.workers)
        path = write_report(by_config, args.label)
        for cfg, rs in by_config.items():
            s = summarise(rs)
            print(f"{cfg:45} block={s['block_rate']:.2f} FP={s['false_positive_rate']:.2f} "
                  f"priv={s['privileged_calls_by_attacks']} cost=${s['mean_cost_usd']:.4f} p95={s['p95_latency_s']}s "
                  f"succeeded={s['succeeded']} fp={s['false_positives']}")
        print("report:", path)
        return 0

    if args.command == "gap":
        from regtech.entities import EntityNotFound
        from regtech.policy_gap import PolicyGapChecker
        try:
            report = PolicyGapChecker().check({"entity": args.entity, "topic": args.topic, "policy_path": args.file})
        except EntityNotFound as e:
            print(e)
            return 2
        print(report.model_dump_json(indent=2) if args.json else report.render())
        if args.precedents:
            from regtech.precedent import precedents_for_gaps
            print("\n" + "=" * 78 + "\nHas the RBI penalised these gaps before?\n" + "=" * 78)
            for finding, prec in precedents_for_gaps(report.model_dump()):
                print(f"\n* [{finding['status'].upper()}] {finding['requirement']}")
                print("  " + prec.render().split("\n", 2)[-1].replace("\n", "\n  "))
        return 0

    if args.command == "build-cases":
        from regtech.enforcement import build_cases
        print(f"{len(build_cases())} cases written")
        return 0

    if args.command == "precedent":
        from regtech.precedent import PrecedentFinder
        print(PrecedentFinder().find({"risk": args.risk}).render())
        return 0

    if args.command == "eval-deadlines":
        from regtech.deadline_eval import run as run_dl
        out = run_dl()
        for r in out["rows"]:
            print(f"{r['variant']:8} P={r['precision']:.2f} R={r['recall']:.2f} F1={r['f1']:.2f} "
                  f"fields={r['field_accuracy']:.2f} cost=${r['cost_usd']:.4f}")
        print("best:", out["best"])
        return 0

    if args.command == "audit-calendar":
        from regtech.deadline_eval import AUDIT_CSV, write_audit
        print(f"{len(write_audit(args.n))} entries written to {AUDIT_CSV}")
        return 0

    if args.command == "build-calendar":
        from regtech.deadlines import build_calendar
        recs = build_calendar(args.prompt, args.tier)
        from collections import Counter
        print(f"{len(recs)} deadlines:", dict(Counter(r.kind for r in recs)))
        return 0

    if args.command == "upcoming":
        from datetime import date as _date

        from regtech.deadlines import check_upcoming
        req = {"days": args.days, "as_of": _date.fromisoformat(args.as_of) if args.as_of else None, "doc_ids": args.doc}
        print(check_upcoming(req).render())
        return 0

    if args.command == "deadlines":
        from regtech.deadlines import extract_deadlines
        from regtech.manifest import load_manifest
        doc_type = next((r.doc_type for r in load_manifest() if r.doc_id == args.doc_id), None)
        if doc_type is None:
            print(f"unknown doc_id {args.doc_id!r}")
            return 2
        for r in extract_deadlines(doc_type, {args.doc_id}):
            detail = r.due_date or (f"{r.frequency}" + (f" x{r.n_years}" if r.n_years else "")) if r.kind != "event_triggered" \
                else f"{r.window()} of {r.trigger}"
            print(f"[{r.kind}] {detail}: {r.obligation}\n    {r.source}\n    \"{r.quote[:200]}\"")
        return 0

    if args.command == "eval-precedent":
        from regtech.precedent_eval import run as run_prec
        out = run_prec()
        print({k: round(v, 3) for k, v in out["aggregate"].items() if v == v})
        return 0

    if args.command == "eval-gap":
        from regtech.gap_eval import run
        out = run(args.label, args.tier, args.assess_tier)
        print({k: round(v, 3) for k, v in out["aggregate"].items() if v == v}, out["diagnosis"])
        return 0

    if args.command == "ask":
        from regtech.qa import RegulationQA
        print(RegulationQA("strict" if args.strict else "balanced").ask(args.question).render())
        return 0

    if args.command == "qa-tier":
        from regtech.qa_eval import tier_check
        out = tier_check(args.tier, args.label or args.tier.lower())
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in out.items()})
        return 0

    if args.command == "eval-qa":
        from regtech.qa_eval import main as eval_qa
        eval_qa(judge=not args.no_judge)
        print("wrote reports/" + ("stage2_qa_nojudge.md" if args.no_judge else "stage2_qa.md"))
        return 0

    if args.command == "judge-kappa":
        from regtech.qa_eval import judge_kappa
        k = judge_kappa()
        if not k:
            print("no kappa yet: fill in at least 10 rows of the human_* columns in reports/stage2_judge_calibration.csv")
            return 1
        for crit, v in k.items():
            print(f"{crit:13} kappa={v['cohens_kappa']:.3f} raw_agreement={v['raw_agreement']:.3f} n={int(v['n'])}")
        return 0

    if args.command == "eval-retrieval":
        from regtech.retrieval_sweep import CORPORA, main as sweep
        sweep((args.corpus,) if args.corpus else CORPORA)
        return 0

    if args.command == "ingest":
        from regtech.ingest import ingest
        ingest(only=args.only)
        return 0

    if args.command == "check":
        from regtech.check import run
        return run(live=args.live)

    if args.command == "init-manifest":
        from regtech.manifest import init_manifest
        from regtech.paths import MANIFEST_PATH
        created = init_manifest()
        print(f"{'created' if created else 'already exists'}: {MANIFEST_PATH}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
