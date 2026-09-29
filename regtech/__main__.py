import argparse
import logging
import sys


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

    p_egap = sub.add_parser("eval-gap", help="Stage 3 evaluation of check_policy_gap; writes reports/stage3_policy_gap_<label>.*")
    p_egap.add_argument("--label", default="current")

    args = parser.parse_args(argv)

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

    if args.command == "eval-precedent":
        from regtech.precedent_eval import run as run_prec
        out = run_prec()
        print({k: round(v, 3) for k, v in out["aggregate"].items() if v == v})
        return 0

    if args.command == "eval-gap":
        from regtech.gap_eval import run
        out = run(args.label)
        print({k: round(v, 3) for k, v in out["aggregate"].items() if v == v}, out["diagnosis"])
        return 0

    if args.command == "ask":
        from regtech.qa import RegulationQA
        print(RegulationQA("strict" if args.strict else "balanced").ask(args.question).render())
        return 0

    if args.command == "eval-qa":
        from regtech.qa_eval import main as eval_qa
        eval_qa(judge=not args.no_judge)
        print("wrote reports/stage2_qa.md")
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
