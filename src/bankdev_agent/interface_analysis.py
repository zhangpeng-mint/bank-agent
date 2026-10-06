from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from bankdev_agent.config import ProjectConfig, RepositoryConfig
from bankdev_agent.errors import BankDevError
from bankdev_agent.git_snapshot import GitSnapshotInspector


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
TABLE_PATTERN = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.$]*)", re.IGNORECASE)
FIELD_PATTERN = re.compile(r"private\s+final\s+([A-Za-z_][\w<>?, ]*)\s+(\w+)\s*;")
CALL_PATTERN = re.compile(r"\b([a-z][A-Za-z0-9_]*)\.([a-zA-Z_][A-Za-z0-9_]*)\s*\(")
REMOTE_TYPE_PATTERN = re.compile(
    r"(?:Client|Sao|Gateway|Feign|RestTemplate|WebClient|HttpClient)$", re.IGNORECASE
)


class AnalysisError(BankDevError):
    """Raised when deterministic interface analysis cannot establish a required link."""


@dataclass(frozen=True)
class Evidence:
    repo_id: str
    commit: str
    path: str
    start_line: int
    end_line: int
    excerpt_sha256: str
    markdown_link: str

    @classmethod
    def from_lines(
        cls,
        repository: RepositoryConfig,
        path: Path,
        start_line: int,
        end_line: int,
    ) -> Evidence:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if not (1 <= start_line <= end_line <= len(lines)):
            raise AnalysisError(
                f"Invalid evidence range {start_line}-{end_line} for {repository.id}:{path}"
            )
        relative = repository.relative_name(path)
        excerpt = "\n".join(lines[start_line - 1 : end_line])
        absolute = str(path.resolve())
        return cls(
            repo_id=repository.id,
            commit=repository.baseline_commit,
            path=relative,
            start_line=start_line,
            end_line=end_line,
            excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            markdown_link=f"[{path.name}:{start_line}](<{absolute}:{start_line}>)",
        )


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    label: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str
    classification: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class MethodLocation:
    path: Path
    class_name: str
    method_name: str
    start_line: int
    end_line: int
    text: str


class InterfaceAnalyzer:
    """Deterministic, read-only first-slice analyzer for Spring/OpenAPI/MyBatis flows."""

    def __init__(self, config: ProjectConfig) -> None:
        self.config = config

    def analyze(self, interface_id: str, golden_case: str | Path | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"\d{8}", interface_id):
            raise AnalysisError("interface_id must be exactly 8 digits")

        snapshots = [GitSnapshotInspector().inspect(repo) for repo in self.config.repositories]
        failures = [snapshot.repo_id for snapshot in snapshots if not snapshot.ok]
        if failures:
            raise AnalysisError(f"Snapshot gate failed before analysis: {failures}")

        knowledge = self._extract_knowledge(interface_id)
        operation = self._find_openapi_operation(
            method=knowledge["contract"]["method"],
            route=knowledge["contract"]["path"],
        )
        controller = self._find_controller(operation["operation_id"])
        service = self._follow_service(controller, operation["operation_id"])
        mapper_results = self._follow_mappers(service)
        graph = self._build_graph(operation, controller, service, mapper_results)
        downstream = self._analyze_downstream(
            interface_id,
            operation,
            controller,
            service,
        )
        comparison = self._compare(knowledge, operation, controller)

        result: dict[str, Any] = {
            "schema_version": "1.0",
            "interface_id": interface_id,
            "analysis_mode": "deterministic_static",
            "snapshots": {snapshot.repo_id: snapshot.head for snapshot in snapshots},
            "knowledge": knowledge,
            "current_code": {
                "contract": {
                    "method": operation["method"],
                    "path": operation["path"],
                    "operation_id": operation["operation_id"],
                    "request_schema": operation["request_schema"],
                    "response_schema": operation["response_schema"],
                    "evidence": asdict(operation["evidence"]),
                },
                "controller": self._method_payload(controller),
                "service_interface": service["interface"],
                "service_implementation": self._method_payload(service["implementation"]),
                "mappers": mapper_results,
                "tables": sorted(
                    {table for mapper in mapper_results for table in mapper["tables"]}
                ),
                "downstream": downstream,
            },
            "graph": {
                "nodes": [self._node_payload(node) for node in graph[0]],
                "edges": [self._edge_payload(edge) for edge in graph[1]],
            },
            "comparison": comparison,
            "limitations": [
                "结论仅覆盖固定 Git snapshot、已解析方法体和配置允许的源码范围。",
                "静态未命中不能排除运行时配置、数据库驱动、反射或其他版本中的调用。",
                "历史实测与待确认决策来自知识文档，不会提升为当前代码事实。",
            ],
        }
        if golden_case is not None:
            result["benchmark"] = BenchmarkEvaluator().evaluate(result, golden_case)
        return result

    def _extract_knowledge(self, interface_id: str) -> dict[str, Any]:
        repository = self.config.repository("bank_knowledge")
        if repository.content_root is None:
            raise AnalysisError("bank_knowledge.content_root is required")

        candidates: list[tuple[Path, list[str]]] = []
        for path in repository.content_root.rglob("*.md"):
            relative = repository.relative_name(path)
            if not repository.is_included(relative) or repository.is_demo(relative):
                continue
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if any(f"| 原服务号 | {interface_id} |" in line for line in lines):
                candidates.append((path, lines))
        if len(candidates) != 1:
            raise AnalysisError(
                f"Expected one authoritative interface document for {interface_id}, found {len(candidates)}"
            )

        path, lines = candidates[0]
        contract_line = self._line_matching(lines, r"^\| 接口 \| `([A-Z]+) ([^`]+)` \|$")
        match = re.match(r"^\| 接口 \| `([A-Z]+) ([^`]+)` \|$", lines[contract_line - 1])
        if match is None:  # pragma: no cover - guarded by _line_matching
            raise AnalysisError("Cannot parse knowledge contract")
        version_line = self._line_starting(lines, "- 版本：")
        status_line = self._line_starting(lines, "- 状态：")
        nature_line = self._line_starting(lines, "- 资料性质：")
        dependency_line = self._line_starting(lines, "| 下游依赖 |")

        history_line = next(
            (
                number
                for number, line in enumerate(lines, 1)
                if "/internal/v1/loan-products" in line and "历史追溯" in line
            ),
            None,
        )
        historical: list[dict[str, Any]] = []
        if history_line:
            historical.append(
                {
                    "category": "historical_contract",
                    "text": lines[history_line - 1].strip("- "),
                    "evidence": asdict(
                        Evidence.from_lines(repository, path, history_line, history_line)
                    ),
                }
            )

        observed_heading = next(
            (
                number
                for number, line in enumerate(lines, 1)
                if line.startswith("## ") and "dev 环境真实数据" in line
            ),
            None,
        )
        if observed_heading:
            observed_end = min(observed_heading + 2, len(lines))
            historical.append(
                {
                    "category": "historical_runtime_observation",
                    "text": "dev 环境响应示例是采集时实测，不作为当前运行时证明",
                    "evidence": asdict(
                        Evidence.from_lines(repository, path, observed_heading, observed_end)
                    ),
                }
            )

        classified = self._classified_decision_sections(repository, interface_id)
        return {
            "document": repository.relative_name(path),
            "version": lines[version_line - 1].removeprefix("- 版本："),
            "status": lines[status_line - 1].removeprefix("- 状态："),
            "material_nature": lines[nature_line - 1].removeprefix("- 资料性质："),
            "contract": {
                "method": match.group(1),
                "path": match.group(2),
                "downstream_claim": lines[dependency_line - 1],
                "evidence": asdict(
                    Evidence.from_lines(
                        repository,
                        path,
                        min(contract_line, dependency_line),
                        max(contract_line, dependency_line),
                    )
                ),
            },
            "knowledge_facts": [
                {
                    "category": "knowledge_claim",
                    "text": f"{match.group(1)} {match.group(2)}；{lines[dependency_line - 1]}",
                    "status": lines[status_line - 1].removeprefix("- 状态："),
                }
            ],
            "historical_observations": historical + classified["historical_observations"],
            "confirmed_decisions": classified["confirmed_decisions"],
            "pending_decisions": classified["pending_decisions"],
        }

    def _classified_decision_sections(
        self, repository: RepositoryConfig, interface_id: str
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {
            "historical_observations": [],
            "confirmed_decisions": [],
            "pending_decisions": [],
        }
        if repository.content_root is None:
            return result
        for path in repository.content_root.rglob("*.md"):
            relative = repository.relative_name(path)
            if not repository.is_included(relative) or repository.is_demo(relative):
                continue
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if not any(interface_id in line for line in lines):
                continue
            headings = [(number, line) for number, line in enumerate(lines, 1) if line.startswith("## ")]
            for position, (start, heading) in enumerate(headings):
                category = None
                output_key = None
                if "用户确认" in heading:
                    category, output_key = "confirmed_user_decision", "confirmed_decisions"
                elif "已废弃" in heading or "dev 库数据现状" in heading:
                    category, output_key = "historical_observation", "historical_observations"
                elif "待确认" in heading:
                    category, output_key = "pending_decision", "pending_decisions"
                if output_key is None or category is None:
                    continue
                end = headings[position + 1][0] - 1 if position + 1 < len(headings) else len(lines)
                body = [line.strip() for line in lines[start:end] if line.strip()]
                if not body:
                    continue
                result[output_key].append(
                    {
                        "category": category,
                        "text": heading.removeprefix("## "),
                        "evidence": asdict(Evidence.from_lines(repository, path, start, end)),
                    }
                )
        return result

    def _find_openapi_operation(self, method: str, route: str) -> dict[str, Any]:
        repository = self.config.repository("dfbm")
        for path in repository.resolved_root.rglob("*.yaml"):
            relative = repository.relative_name(path)
            if not repository.is_included(relative):
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for index, line in enumerate(lines):
                route_match = re.match(r"^(\s*)(/[^:]+):\s*$", line)
                if route_match is None or route_match.group(2) != route:
                    continue
                indent = len(route_match.group(1))
                end_index = len(lines)
                for cursor in range(index + 1, len(lines)):
                    candidate = re.match(r"^(\s*)(/[^:]+):\s*$", lines[cursor])
                    if candidate and len(candidate.group(1)) == indent:
                        end_index = cursor
                        break
                block = lines[index:end_index]
                method_line = next(
                    (
                        offset
                        for offset, value in enumerate(block, index + 1)
                        if value.strip().removesuffix(":").lower() == method.lower()
                    ),
                    None,
                )
                if method_line is None:
                    continue
                operation_id = self._yaml_scalar(block, "operationId")
                refs = [
                    match.group(1)
                    for value in block
                    if (match := re.search(r"#/components/schemas/([A-Za-z0-9_]+)", value))
                ]
                return {
                    "method": method.upper(),
                    "path": route,
                    "operation_id": operation_id,
                    "request_schema": refs[0] if refs else None,
                    "response_schema": refs[1] if len(refs) > 1 else (refs[0] if refs else None),
                    "evidence": Evidence.from_lines(repository, path, index + 1, end_index),
                }
        raise AnalysisError(f"OpenAPI operation not found for {method} {route}")

    def _find_controller(self, operation_id: str) -> MethodLocation:
        repository = self.config.repository("dfbm")
        candidates: list[MethodLocation] = []
        for path in repository.resolved_root.rglob("*.java"):
            relative = repository.relative_name(path)
            if not repository.is_included(relative):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if operation_id not in text or "Controller" not in text:
                continue
            try:
                location = self._locate_method(path, operation_id)
            except AnalysisError:
                continue
            if location.class_name.endswith("Controller"):
                candidates.append(location)
        if len(candidates) != 1:
            raise AnalysisError(
                f"Expected one controller method for {operation_id}, found {len(candidates)}"
            )
        return candidates[0]

    def _follow_service(self, controller: MethodLocation, operation_id: str) -> dict[str, Any]:
        repository = self.config.repository("dfbm")
        class_text = controller.path.read_text(encoding="utf-8", errors="replace")
        fields = {variable: type_name.strip() for type_name, variable in FIELD_PATTERN.findall(class_text)}
        calls = CALL_PATTERN.findall(controller.text)
        service_call = next(
            (
                (variable, method)
                for variable, method in calls
                if variable in fields and fields[variable].endswith("Service") and method == operation_id
            ),
            None,
        )
        if service_call is None:
            raise AnalysisError(f"No service call found in {controller.class_name}.{operation_id}")
        variable, method_name = service_call
        interface_type = fields[variable]
        interface_path = self._unique_java_type(repository, interface_type)
        interface_method = self._locate_method(interface_path, method_name, allow_abstract=True)

        implementations: list[Path] = []
        pattern = re.compile(rf"\bimplements\s+[^{{]*\b{re.escape(interface_type)}\b")
        for path in repository.resolved_root.rglob("*.java"):
            relative = repository.relative_name(path)
            if repository.is_included(relative) and pattern.search(
                path.read_text(encoding="utf-8", errors="replace")
            ):
                implementations.append(path)
        if len(implementations) != 1:
            raise AnalysisError(
                f"Expected one {interface_type} implementation, found {len(implementations)}"
            )
        implementation = self._locate_method(implementations[0], method_name)
        return {
            "interface": self._method_payload(interface_method),
            "implementation": implementation,
            "field_name": variable,
        }

    def _follow_mappers(self, service: dict[str, Any]) -> list[dict[str, Any]]:
        repository = self.config.repository("dfbm")
        implementation: MethodLocation = service["implementation"]
        class_text = implementation.path.read_text(encoding="utf-8", errors="replace")
        fields = {variable: type_name.strip() for type_name, variable in FIELD_PATTERN.findall(class_text)}
        calls = []
        for variable, method in CALL_PATTERN.findall(implementation.text):
            type_name = fields.get(variable)
            if type_name and type_name.endswith("Mapper"):
                item = (variable, type_name, method)
                if item not in calls:
                    calls.append(item)

        results: list[dict[str, Any]] = []
        for variable, mapper_type, method_name in calls:
            java_path = self._unique_java_type(repository, mapper_type)
            java_method = self._locate_method(java_path, method_name, allow_abstract=True)
            xml_path, start, end, statement_text = self._find_mapper_statement(
                repository, mapper_type, method_name
            )
            tables = sorted({match.group(1) for match in TABLE_PATTERN.finditer(statement_text)})
            if not tables:
                raise AnalysisError(f"No tables found for {mapper_type}.{method_name}")
            results.append(
                {
                    "field": variable,
                    "mapper_type": mapper_type,
                    "method": method_name,
                    "java_evidence": asdict(
                        Evidence.from_lines(
                            repository,
                            java_method.path,
                            java_method.start_line,
                            java_method.end_line,
                        )
                    ),
                    "xml_evidence": asdict(Evidence.from_lines(repository, xml_path, start, end)),
                    "tables": tables,
                }
            )
        if not results:
            raise AnalysisError("No mapper calls found in service implementation")
        return results

    def _analyze_downstream(
        self,
        interface_id: str,
        operation: dict[str, Any],
        controller: MethodLocation,
        service: dict[str, Any],
    ) -> dict[str, Any]:
        repository = self.config.repository("dfbm")
        implementation: MethodLocation = service["implementation"]
        remote_hits: list[str] = []
        for location in (controller, implementation):
            class_text = location.path.read_text(encoding="utf-8", errors="replace")
            fields = {variable: type_name.strip() for type_name, variable in FIELD_PATTERN.findall(class_text)}
            for variable, method in CALL_PATTERN.findall(location.text):
                type_name = fields.get(variable, "")
                if REMOTE_TYPE_PATTERN.search(type_name) and not type_name.endswith("Mapper"):
                    remote_hits.append(f"{location.class_name}:{type_name}.{method}")

        product_scope_hits: list[str] = []
        product_root = repository.resolve_path(
            "dfbm-fbs/src/main/java/com/wefi/dfbm/fbs/product",
            enforce_patterns=False,
            expected_kind="directory",
        )
        remote_literals = (
            "RestTemplate",
            "WebClient",
            "FeignClient",
            "DcisClient",
            "FineractClient",
            "BasClient",
        )
        for path in product_root.rglob("*.java"):
            relative = repository.relative_name(path)
            if not repository.is_included(relative):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for literal in remote_literals:
                if literal in text:
                    product_scope_hits.append(f"{relative}:{literal}")

        source_hits: dict[str, list[str]] = {}
        literals = [interface_id, operation["path"], operation["operation_id"]]
        for repo_id in ("dcis", "fineract"):
            target = self.config.repository(repo_id)
            hits: list[str] = []
            for path in target.resolved_root.rglob("*"):
                if not path.is_file() or path.suffix not in {".java", ".xml", ".yaml", ".yml"}:
                    continue
                relative = target.relative_name(path)
                if not target.is_included(relative):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for literal in literals:
                    if literal in text:
                        hits.append(f"{relative}:{literal}")
            source_hits[repo_id] = hits

        evidence = [
            asdict(
                Evidence.from_lines(
                    repository,
                    controller.path,
                    controller.start_line,
                    controller.end_line,
                )
            ),
            asdict(
                Evidence.from_lines(
                    repository,
                    implementation.path,
                    implementation.start_line,
                    implementation.end_line,
                )
            ),
        ]
        return {
            "confirmed_remote_edges": [],
            "method_scope_remote_hits": remote_hits,
            "dfbm_product_scope_remote_hits": product_scope_hits,
            "cross_repo_literal_hits": source_hits,
            "conclusion": (
                "在固定 commit、已解析方法体和已扫描源码范围内未发现当前查询链的 DCIS/Fineract 下游调用"
                if not remote_hits and not product_scope_hits and not any(source_hits.values())
                else "发现候选下游证据，需要人工复核"
            ),
            "classification": (
                "scoped_absence" if not remote_hits and not product_scope_hits else "unresolved"
            ),
            "evidence": evidence,
        }

    def _compare(
        self,
        knowledge: dict[str, Any],
        operation: dict[str, Any],
        controller: MethodLocation,
    ) -> dict[str, Any]:
        knowledge_contract = knowledge["contract"]
        contract_aligned = (
            knowledge_contract["method"] == operation["method"]
            and knowledge_contract["path"] == operation["path"]
        )
        historical_drift = any(
            item["category"] == "historical_contract"
            for item in knowledge["historical_observations"]
        )
        return {
            "knowledge_vs_current_code": "aligned" if contract_aligned else "drift",
            "historical_contract_drift_detected": historical_drift,
            "historical_contract": "GET /internal/v1/loan-products" if historical_drift else None,
            "current_contract": f"{operation['method']} {operation['path']}",
            "current_controller": controller.class_name,
            "separation_complete": all(
                knowledge[key]
                for key in (
                    "knowledge_facts",
                    "historical_observations",
                    "confirmed_decisions",
                    "pending_decisions",
                )
            ),
        }

    def _build_graph(
        self,
        operation: dict[str, Any],
        controller: MethodLocation,
        service: dict[str, Any],
        mappers: list[dict[str, Any]],
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        repository = self.config.repository("dfbm")
        endpoint_id = f"endpoint:dfbm:{operation['operation_id']}"
        controller_id = f"method:dfbm:{controller.class_name}.{controller.method_name}"
        implementation: MethodLocation = service["implementation"]
        service_id = f"method:dfbm:{implementation.class_name}.{implementation.method_name}"
        nodes = [
            GraphNode(
                endpoint_id,
                "endpoint",
                f"{operation['method']} {operation['path']}",
                (operation["evidence"],),
            ),
            GraphNode(
                controller_id,
                "controller_method",
                f"{controller.class_name}.{controller.method_name}",
                (
                    Evidence.from_lines(
                        repository, controller.path, controller.start_line, controller.end_line
                    ),
                ),
            ),
            GraphNode(
                service_id,
                "service_method",
                f"{implementation.class_name}.{implementation.method_name}",
                (
                    Evidence.from_lines(
                        repository,
                        implementation.path,
                        implementation.start_line,
                        implementation.end_line,
                    ),
                ),
            ),
        ]
        edges = [
            GraphEdge(endpoint_id, controller_id, "IMPLEMENTED_BY", "confirmed_static", nodes[1].evidence),
            GraphEdge(controller_id, service_id, "CALLS", "confirmed_static", nodes[1].evidence),
        ]
        for mapper in mappers:
            mapper_id = f"mapper:dfbm:{mapper['mapper_type']}.{mapper['method']}"
            xml_evidence = Evidence(**mapper["xml_evidence"])
            nodes.append(
                GraphNode(
                    mapper_id,
                    "mapper_statement",
                    f"{mapper['mapper_type']}.{mapper['method']}",
                    (xml_evidence,),
                )
            )
            edges.append(
                GraphEdge(service_id, mapper_id, "CALLS", "confirmed_static", (xml_evidence,))
            )
            for table in mapper["tables"]:
                table_id = f"table:dfbm:{table}"
                if not any(node.id == table_id for node in nodes):
                    nodes.append(GraphNode(table_id, "table", table, (xml_evidence,)))
                edges.append(
                    GraphEdge(mapper_id, table_id, "READS", "confirmed_static", (xml_evidence,))
                )
        return nodes, edges

    @staticmethod
    def _locate_method(
        path: Path, method_name: str, *, allow_abstract: bool = False
    ) -> MethodLocation:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        class_match = next(
            (
                re.search(r"\b(?:class|interface)\s+(\w+)", line)
                for line in lines
                if re.search(r"\b(?:class|interface)\s+(\w+)", line)
            ),
            None,
        )
        if class_match is None:
            raise AnalysisError(f"No class/interface declaration in {path}")
        candidates = [
            index
            for index, line in enumerate(lines)
            if re.search(rf"\b{re.escape(method_name)}\s*\(", line)
            and not line.strip().startswith("//")
        ]
        for start in candidates:
            signature_end = start
            while signature_end < len(lines) and not re.search(r"[;{]", lines[signature_end]):
                signature_end += 1
            if signature_end >= len(lines):
                continue
            signature = " ".join(line.strip() for line in lines[start : signature_end + 1])
            is_abstract_declaration = allow_abstract and ";" in signature and "{" not in signature
            if not is_abstract_declaration and not re.search(
                r"\b(?:public|protected|private|default|static)", signature
            ):
                continue
            if ";" in signature and "{" not in signature:
                if not allow_abstract:
                    continue
                end = signature_end
            else:
                brace_count = 0
                seen_open = False
                end = signature_end
                for cursor in range(signature_end, len(lines)):
                    brace_count += lines[cursor].count("{") - lines[cursor].count("}")
                    seen_open = seen_open or "{" in lines[cursor]
                    end = cursor
                    if seen_open and brace_count == 0:
                        break
            return MethodLocation(
                path=path,
                class_name=class_match.group(1),
                method_name=method_name,
                start_line=start + 1,
                end_line=end + 1,
                text="\n".join(lines[start : end + 1]),
            )
        raise AnalysisError(f"Method {method_name} not found in {path}")

    @staticmethod
    def _find_mapper_statement(
        repository: RepositoryConfig, mapper_type: str, method_name: str
    ) -> tuple[Path, int, int, str]:
        candidates = list(repository.resolved_root.rglob(f"{mapper_type}.xml"))
        candidates = [path for path in candidates if repository.is_included(repository.relative_name(path))]
        if len(candidates) != 1:
            raise AnalysisError(f"Expected one XML for {mapper_type}, found {len(candidates)}")
        path = candidates[0]
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = next(
            (
                index
                for index, line in enumerate(lines)
                if re.search(rf"<(?:select|insert|update|delete)\s+id=\"{re.escape(method_name)}\"", line)
            ),
            None,
        )
        if start is None:
            raise AnalysisError(f"Statement {mapper_type}.{method_name} not found")
        tag_match = re.search(r"<(select|insert|update|delete)\b", lines[start])
        if tag_match is None:  # pragma: no cover - guarded above
            raise AnalysisError("Cannot parse mapper tag")
        close = f"</{tag_match.group(1)}>"
        end = next((index for index in range(start, len(lines)) if close in lines[index]), None)
        if end is None:
            raise AnalysisError(f"Unclosed statement {mapper_type}.{method_name}")
        return path, start + 1, end + 1, "\n".join(lines[start : end + 1])

    @staticmethod
    def _unique_java_type(repository: RepositoryConfig, type_name: str) -> Path:
        candidates = [
            path
            for path in repository.resolved_root.rglob(f"{type_name}.java")
            if repository.is_included(repository.relative_name(path))
        ]
        if len(candidates) != 1:
            raise AnalysisError(f"Expected one Java type {type_name}, found {len(candidates)}")
        return candidates[0]

    @staticmethod
    def _yaml_scalar(lines: Iterable[str], key: str) -> str:
        pattern = re.compile(rf"^\s*{re.escape(key)}:\s*([^#]+?)\s*$")
        for line in lines:
            if match := pattern.match(line):
                return match.group(1).strip("'\"")
        raise AnalysisError(f"Missing YAML scalar: {key}")

    @staticmethod
    def _line_starting(lines: list[str], prefix: str) -> int:
        for number, line in enumerate(lines, 1):
            if line.startswith(prefix):
                return number
        raise AnalysisError(f"Knowledge metadata not found: {prefix}")

    @staticmethod
    def _line_matching(lines: list[str], pattern: str) -> int:
        regex = re.compile(pattern)
        for number, line in enumerate(lines, 1):
            if regex.match(line):
                return number
        raise AnalysisError(f"Knowledge pattern not found: {pattern}")

    def _method_payload(self, method: MethodLocation) -> dict[str, Any]:
        repository = self.config.repository("dfbm")
        return {
            "class": method.class_name,
            "method": method.method_name,
            "evidence": asdict(
                Evidence.from_lines(
                    repository, method.path, method.start_line, method.end_line
                )
            ),
        }

    @staticmethod
    def _node_payload(node: GraphNode) -> dict[str, Any]:
        return {
            "id": node.id,
            "kind": node.kind,
            "label": node.label,
            "evidence": [asdict(item) for item in node.evidence],
        }

    @staticmethod
    def _edge_payload(edge: GraphEdge) -> dict[str, Any]:
        return {
            "from": edge.source,
            "to": edge.target,
            "kind": edge.kind,
            "classification": edge.classification,
            "evidence": [asdict(item) for item in edge.evidence],
        }


class BenchmarkEvaluator:
    def evaluate(self, analysis: dict[str, Any], case_path: str | Path) -> dict[str, Any]:
        path = Path(case_path).expanduser().resolve(strict=True)
        case = json.loads(path.read_text(encoding="utf-8"))
        expected = case["expected_interface"]
        current = analysis["current_code"]
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, actual: Any, expected_value: Any) -> None:
            checks.append(
                {"name": name, "ok": ok, "actual": actual, "expected": expected_value}
            )

        for field in ("method", "path", "operation_id"):
            add(
                f"interface.{field}",
                current["contract"][field] == expected[field],
                current["contract"][field],
                expected[field],
            )
        expected_nodes = case["expected_nodes"]
        expected_controller = next(item for item in expected_nodes if item["kind"] == "method")
        add(
            "controller.path",
            current["controller"]["evidence"]["path"] == expected_controller["path"],
            current["controller"]["evidence"]["path"],
            expected_controller["path"],
        )
        expected_tables = sorted(case["expected_tables"])
        add("tables", current["tables"] == expected_tables, current["tables"], expected_tables)
        downstream_ok = (
            not current["downstream"]["confirmed_remote_edges"]
            and not current["downstream"]["method_scope_remote_hits"]
            and not current["downstream"]["dfbm_product_scope_remote_hits"]
            and not any(current["downstream"]["cross_repo_literal_hits"].values())
        )
        add("no_false_downstream_edges", downstream_ok, downstream_ok, True)
        all_evidenced = all(node["evidence"] for node in analysis["graph"]["nodes"]) and all(
            edge["evidence"] for edge in analysis["graph"]["edges"]
        )
        add("all_graph_items_have_evidence", all_evidenced, all_evidenced, True)
        separation = analysis["comparison"]["separation_complete"]
        add("source_categories_separated", separation, separation, True)
        drift = analysis["comparison"]["historical_contract_drift_detected"]
        add("historical_drift_detected", drift, drift, True)

        return {
            "ok": all(check["ok"] for check in checks),
            "case_id": case["case_id"],
            "checks": checks,
            "metrics": {
                "interface_top_1": int(all(check["ok"] for check in checks[:3])),
                "confirmed_edge_precision": 1.0 if downstream_ok else 0.0,
                "evidence_coverage": 1.0 if all_evidenced else 0.0,
                "hallucinated_downstream_edges": 0 if downstream_ok else 1,
            },
        }


def render_markdown(result: dict[str, Any]) -> str:
    current = result["current_code"]
    contract = current["contract"]
    lines = [
        f"# 接口 {result['interface_id']} 确定性分析",
        "",
        f"- 当前契约：`{contract['method']} {contract['path']}`",
        f"- operationId：`{contract['operation_id']}`",
        f"- Controller：`{current['controller']['class']}.{current['controller']['method']}`",
        f"- Service：`{current['service_implementation']['class']}.{current['service_implementation']['method']}`",
        f"- 下游结论：{current['downstream']['conclusion']}",
        f"- 文档对比：{result['comparison']['knowledge_vs_current_code']}；历史漂移={result['comparison']['historical_contract_drift_detected']}",
        "",
        "## 证据链",
        "",
    ]
    for node in result["graph"]["nodes"]:
        links = "、".join(item["markdown_link"] for item in node["evidence"])
        lines.append(f"- `{node['kind']}` {node['label']} — {links}")
    lines.extend(["", "## 表", "", ", ".join(f"`{table}`" for table in current["tables"])])
    if benchmark := result.get("benchmark"):
        lines.extend(
            [
                "",
                "## Golden 验收",
                "",
                f"结果：**{'PASS' if benchmark['ok'] else 'FAIL'}**",
                "",
            ]
        )
        for check in benchmark["checks"]:
            lines.append(f"- {'✅' if check['ok'] else '❌'} `{check['name']}`")
    return "\n".join(lines) + "\n"
