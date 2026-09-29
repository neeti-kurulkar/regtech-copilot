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

    args = parser.parse_args(argv)

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
