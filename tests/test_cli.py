from covledger.cli import build_parser


def test_flat_root_option_and_runs_query_parse() -> None:
    args = build_parser().parse_args(["--root", "/tmp/project", "runs", "latest", "gaps", "--limit", "5"])
    assert args.root == "/tmp/project"
    assert args.selector == "latest"
    assert args.action == "gaps"
    assert args.limit == 5
