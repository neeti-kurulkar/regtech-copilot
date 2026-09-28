import argparse
import sys


def main(argv: list[str] | None = None) -> int:
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

    args = parser.parse_args(argv)

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
