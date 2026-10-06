from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bankdev_agent.config import ProjectConfig, load_project_config
from bankdev_agent.code_index import CodeIndexer
from bankdev_agent.errors import BankDevError, ScopeViolation
from bankdev_agent.git_snapshot import GitSnapshotInspector
from bankdev_agent.golden import GoldenVerifier
from bankdev_agent.index_query import IndexQuery
from bankdev_agent.interface_analysis import InterfaceAnalyzer, render_markdown
from bankdev_agent.knowledge_index import KnowledgeIndexer
from bankdev_agent.knowledge_conflicts import KnowledgeConflictQuery
from bankdev_agent.knowledge_query import KnowledgeQuery
from bankdev_agent.requirement_analysis import RequirementAnalyzer, RequirementFrame, render_requirement_markdown
from bankdev_agent.model_gateway import ModelGateway, MockBackend, ZhipuBackend, DEFAULT_MODEL
from bankdev_agent.model_analysis import AssistedRequirementAnalyzer, smoke_test


DEFAULT_CONFIG = Path("config/repositories.toml")
DEFAULT_INDEX = Path("var/bankdev.sqlite3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bankdev",
        description="BankDev Agent read-only verification and deterministic analysis tools.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Repository scope configuration (default: config/repositories.toml)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("check-config", help="Parse and validate the read-only configuration")

    requirement = subcommands.add_parser("analyze-requirement", help="Run deterministic requirement analysis and evidence gates")
    requirement.add_argument("text", help="Requirement goal; eight-digit interface ids are extracted literally")
    for option, dest in (("non-goal", "non_goals"), ("business-object", "business_objects"),
                         ("interface-id", "interface_ids"), ("term", "terms"), ("constraint", "constraints"),
                         ("acceptance", "acceptance_criteria"), ("question", "open_questions"),
                         ("decision", "user_decisions")):
        requirement.add_argument("--" + option, action="append", dest=dest, default=[])
    requirement.add_argument("--max-depth", type=int, default=8)
    requirement.add_argument("--max-nodes", type=int, default=200)
    requirement.add_argument("--db", type=Path, default=DEFAULT_INDEX)
    requirement.add_argument("--format", choices=("json", "markdown"), default="json")
    requirement.add_argument("--output", type=Path)
    requirement.add_argument("--backend", choices=("template", "mock", "zhipu"), default="template")
    requirement.add_argument("--model", default=DEFAULT_MODEL)
    requirement.add_argument("--model-timeout", type=float, default=30)
    requirement.add_argument("--model-context", choices=("requirement-only", "verified-facts"), default="requirement-only",
                             help="verified-facts explicitly sends up to 40 validated fact texts/IDs, never source files")
    smoke = subcommands.add_parser("model-smoke", help="Test model intake with fixed synthetic data only")
    smoke.add_argument("--backend", choices=("mock", "zhipu"), default="mock")
    smoke.add_argument("--model", default=DEFAULT_MODEL)
    smoke.add_argument("--model-timeout", type=float, default=30)
    smoke.add_argument("--output", type=Path)

    snapshots = subcommands.add_parser("snapshots", help="Inspect configured Git snapshots")
    snapshots.add_argument("--repo", action="append", dest="repo_ids", help="Limit to a repository id")

    golden = subcommands.add_parser("verify-golden", help="Verify a golden case against current snapshots")
    golden.add_argument("case", type=Path, help="Path to case.json")

    analyze = subcommands.add_parser(
        "analyze-interface",
        help="Reconstruct an interface flow from knowledge, OpenAPI, Java and MyBatis evidence",
    )
    analyze.add_argument("interface_id", help="Eight-digit legacy interface id")
    analyze.add_argument("--golden", type=Path, help="Optional Golden case for evaluation")
    analyze.add_argument("--format", choices=("json", "markdown"), default="json")
    analyze.add_argument("--output", type=Path, help="Write the rendered result to this path")

    index_code = subcommands.add_parser(
        "index-code", help="Build or incrementally refresh the fixed-snapshot SQLite code index"
    )
    index_code.add_argument("--repo", action="append", dest="repo_ids", help="Limit to a repository id")
    index_code.add_argument("--db", type=Path, default=DEFAULT_INDEX, help="SQLite path under var/")

    index_docs = subcommands.add_parser(
        "index-docs", help="Build or incrementally refresh the authorized knowledge index"
    )
    index_docs.add_argument("--repo", default="bank_knowledge", help="Configured knowledge repository id")
    index_docs.add_argument("--db", type=Path, default=DEFAULT_INDEX, help="SQLite path under var/")

    search = subcommands.add_parser(
        "search-interface", help="Find an indexed interface by legacy id, route or operationId"
    )
    search.add_argument("query", help="Legacy interface id, route fragment or operationId")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--db", type=Path, default=DEFAULT_INDEX, help="SQLite path under var/")

    search_knowledge = subcommands.add_parser(
        "search-knowledge", help="Search active knowledge chunks with evidence and version metadata"
    )
    search_knowledge.add_argument("query", help="Interface id, business term or phrase")
    search_knowledge.add_argument("--system", action="append", dest="systems")
    search_knowledge.add_argument("--limit", type=int, default=10)
    search_knowledge.add_argument("--include-superseded", action="store_true")
    search_knowledge.add_argument("--db", type=Path, default=DEFAULT_INDEX, help="SQLite path under var/")

    conflicts = subcommands.add_parser(
        "detect-conflicts",
        help="Compare field-level knowledge claims and current indexed endpoint contracts",
    )
    conflicts.add_argument("interface_id", nargs="?", help="Optional eight-digit interface id")
    conflicts.add_argument("--db", type=Path, default=DEFAULT_INDEX, help="SQLite path under var/")

    search_code = subcommands.add_parser(
        "search-code", help="Search indexed code symbols, endpoints and evidence"
    )
    search_code.add_argument("query", help="Class, method, qualified name, route or operationId")
    search_code.add_argument("--repo", action="append", dest="repo_ids")
    search_code.add_argument("--kind", action="append", dest="kinds")
    search_code.add_argument("--limit", type=int, default=20)
    search_code.add_argument("--db", type=Path, default=DEFAULT_INDEX, help="SQLite path under var/")

    trace = subcommands.add_parser(
        "trace", help="Traverse indexed endpoint, Java, mapper and SQL-table evidence"
    )
    trace.add_argument("start", help="Legacy id, operationId or indexed node id")
    trace.add_argument("--max-depth", type=int, default=8)
    trace.add_argument("--max-nodes", type=int, default=200)
    trace.add_argument("--direction", choices=("downstream", "upstream", "both"), default="downstream")
    trace.add_argument("--db", type=Path, default=DEFAULT_INDEX, help="SQLite path under var/")

    impact = subcommands.add_parser(
        "impact", help="Find direct and transitive upstream impact candidates"
    )
    impact.add_argument("start", help="OperationId, symbol name or indexed node id")
    impact.add_argument("--max-depth", type=int, default=8)
    impact.add_argument("--max-nodes", type=int, default=200)
    impact.add_argument("--include-tests", action="store_true")
    impact.add_argument("--db", type=Path, default=DEFAULT_INDEX, help="SQLite path under var/")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        config = load_project_config(args.config)
        if args.command == "check-config":
            payload, ok = _check_config(config)
        elif args.command == "model-smoke":
            backend = MockBackend() if args.backend == "mock" else ZhipuBackend(args.model, args.model_timeout)
            payload = smoke_test(ModelGateway(backend))
            ok = payload["ok"]
            if args.output:
                _write_analysis_output(config, args.output, json.dumps(payload, ensure_ascii=False, indent=2))
        elif args.command == "analyze-requirement":
            frame = RequirementFrame.parse(args.text, **{name: getattr(args, name) for name in (
                "non_goals", "business_objects", "interface_ids", "terms", "constraints",
                "acceptance_criteria", "open_questions", "user_decisions")})
            analyzer = RequirementAnalyzer(config, _index_path(config, args.db))
            if args.backend == "template":
                payload = analyzer.analyze(frame, args.max_depth, args.max_nodes)
            else:
                backend = MockBackend() if args.backend == "mock" else ZhipuBackend(args.model, args.model_timeout)
                payload = AssistedRequirementAnalyzer(analyzer, ModelGateway(backend), args.model_context).analyze(
                    frame, args.max_depth, args.max_nodes)
            rendered = (json.dumps(payload, ensure_ascii=False, indent=2) if args.format == "json"
                        else render_requirement_markdown(payload))
            if args.output:
                _write_analysis_output(config, args.output, rendered)
            print(rendered)
            if not payload["ok"]:
                raise SystemExit(1)
            return
        elif args.command == "snapshots":
            payload, ok = _snapshots(config, args.repo_ids)
        elif args.command == "verify-golden":
            report = GoldenVerifier(config).verify(args.case)
            payload, ok = report.to_dict(), report.ok
        elif args.command == "analyze-interface":
            payload = InterfaceAnalyzer(config).analyze(args.interface_id, args.golden)
            ok = payload.get("benchmark", {}).get("ok", True)
            rendered = (
                json.dumps(payload, ensure_ascii=False, indent=2)
                if args.format == "json"
                else render_markdown(payload)
            )
            if args.output:
                _write_analysis_output(config, args.output, rendered)
            print(rendered)
            if not ok:
                raise SystemExit(1)
            return
        elif args.command == "index-code":
            database = _index_path(config, args.db)
            payload = CodeIndexer(config, database).index(args.repo_ids)
            ok = True
        elif args.command == "index-docs":
            database = _index_path(config, args.db)
            payload = KnowledgeIndexer(config, database).index(args.repo)
            ok = True
        elif args.command == "search-interface":
            database = _index_path(config, args.db)
            payload = IndexQuery(config, database).search_interface(args.query, args.limit)
            ok = payload["count"] > 0
        elif args.command == "search-code":
            database = _index_path(config, args.db)
            payload = IndexQuery(config, database).search_code(
                args.query, args.limit, args.repo_ids, args.kinds
            )
            ok = payload["count"] > 0
        elif args.command == "search-knowledge":
            database = _index_path(config, args.db)
            payload = KnowledgeQuery(config, database).search(
                args.query, args.limit, args.systems, args.include_superseded
            )
            ok = payload["count"] > 0
        elif args.command == "detect-conflicts":
            database = _index_path(config, args.db)
            payload = KnowledgeConflictQuery(config, database).report(args.interface_id)
            ok = payload["ok"]
        elif args.command == "trace":
            database = _index_path(config, args.db)
            payload = IndexQuery(config, database).trace(
                args.start, max_depth=args.max_depth, max_nodes=args.max_nodes,
                direction=args.direction,
            )
            ok = True
        elif args.command == "impact":
            database = _index_path(config, args.db)
            payload = IndexQuery(config, database).impact(
                args.start, max_depth=args.max_depth, max_nodes=args.max_nodes,
                include_tests=args.include_tests,
            )
            ok = True
        else:  # pragma: no cover - argparse prevents this branch
            raise AssertionError(f"Unhandled command: {args.command}")
    except (BankDevError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not ok:
        raise SystemExit(1)


def _check_config(config: ProjectConfig) -> tuple[dict[str, object], bool]:
    repositories = []
    for repository in config.repositories:
        repositories.append(
            {
                "id": repository.id,
                "kind": repository.kind,
                "root": str(repository.resolved_root),
                "baseline_branch": repository.baseline_branch,
                "baseline_commit": repository.baseline_commit,
                "include_count": len(repository.include),
                "exclude_count": len(repository.exclude),
                "content_root": str(repository.content_root) if repository.content_root else None,
            }
        )
    return (
        {
            "ok": True,
            "schema_version": config.schema_version,
            "access_mode": config.access_mode,
            "repositories": repositories,
            "safety": {
                "allowed_operations": config.safety.allowed_operations,
                "denied_operations": config.safety.denied_operations,
                "secrets_policy": config.safety.secrets_policy,
            },
        },
        True,
    )


def _snapshots(
    config: ProjectConfig, repo_ids: list[str] | None
) -> tuple[dict[str, object], bool]:
    selected = (
        [config.repository(repo_id) for repo_id in repo_ids]
        if repo_ids
        else list(config.repositories)
    )
    inspector = GitSnapshotInspector()
    reports = [inspector.inspect(repository) for repository in selected]
    ok = all(report.ok for report in reports)
    return {"ok": ok, "snapshots": [report.to_dict() for report in reports]}, ok


def _write_analysis_output(config: ProjectConfig, output: Path, rendered: str) -> None:
    project_root = config.source_path.parent.parent
    output_root = (project_root / "var").resolve(strict=True)
    target = output.resolve() if output.is_absolute() else (project_root / output).resolve()
    if not target.is_relative_to(output_root):
        raise ScopeViolation("Analysis output must stay under the BankDev Agent var/ directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")


def _index_path(config: ProjectConfig, database: Path) -> Path:
    project_root = config.source_path.parent.parent
    output_root = (project_root / "var").resolve(strict=True)
    target = database.resolve() if database.is_absolute() else (project_root / database).resolve()
    if not target.is_relative_to(output_root):
        raise ScopeViolation("SQLite index must stay under the BankDev Agent var/ directory")
    return target


if __name__ == "__main__":
    main(sys.argv[1:])
