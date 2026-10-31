"""Bounded Java 8/Spring/MyBatis declaration analysis.

Tree-sitter supplies Java syntax, not compiler type inference. Every relation is
a static candidate. Overloads, dynamic dispatch, route constants and dynamic SQL
which cannot be determined from this snapshot are returned as explicit gaps.
No Java compiler, repository process, datasource, or network client is invoked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from xml.parsers import expat

import tree_sitter_java
from defusedxml import ElementTree as SafeXML
from defusedxml.common import DefusedXmlException
from tree_sitter import Language, Node, Parser

from .cancellation import checkpoint
from .syntax_cache import unit, encode_java, decode_java, encode_xml, decode_xml

VERSION = "vks-java-spring-mybatis-1"
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE")
MAPPINGS = {"RequestMapping", "GetMapping", "PostMapping", "PutMapping", "PatchMapping", "DeleteMapping"}
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_DECLARATIONS = 100_000
MAX_XML_DEPTH = 64
JAVA_LANGUAGE = Language(tree_sitter_java.language())


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def java_lexical_sha(raw: bytes):
    """Whole-file Java token identity; comments/spacing change no executable tokens."""
    if len(raw) > MAX_SOURCE_BYTES:
        return None
    try:
        source = raw.decode("utf-8-sig").encode()
    except UnicodeError:
        return None
    root = Parser(JAVA_LANGUAGE).parse(source).root_node
    if root.has_error:
        return None
    pending = [root]
    tokens = []
    while pending:
        checkpoint()
        node = pending.pop()
        if node.type in ("line_comment", "block_comment"):
            continue
        if node.children:
            pending.extend(reversed(node.children))
        else:
            tokens.append((node.type, source[node.start_byte : node.end_byte].decode()))
        if len(tokens) > 250000:
            return None
    return sha256(canonical(tokens)).hexdigest()


def stable_key(kind: str, *identity: Any) -> str:
    return f"java.{kind}." + sha256(canonical(identity)).hexdigest()[:40]


def _walk(node: Node):
    stack = [node]
    while stack:
        item = stack.pop()
        yield item
        stack.extend(reversed(item.named_children))


def _text(node: Node | None, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8") if node is not None else ""


def _span(node: Node) -> dict:
    return {"kind": "file_lines", "startLine": node.start_point.row + 1, "endLine": node.end_point.row + 1}


def _annotations(node: Node) -> list[Node]:
    modifiers = next((c for c in node.named_children if c.type == "modifiers"), None)
    return [c for c in modifiers.named_children if c.type in ("annotation", "marker_annotation")] if modifiers else []


def _literal(node: Node | None, source: bytes, constants: dict[str, Any]) -> Any:
    if node is None:
        return None
    if node.type == "string_literal":
        try:
            return json.loads(_text(node, source))
        except (ValueError, TypeError):
            return None
    if node.type in ("element_value_array_initializer", "array_initializer"):
        values = [_literal(c, source, constants) for c in node.named_children]
        return values if all(v is not None for v in values) else None
    if node.type == "identifier":
        return constants.get(_text(node, source))
    if node.type == "field_access":
        text = _text(node, source)
        if re.fullmatch(
            r"(?:org\.springframework\.web\.bind\.annotation\.)?RequestMethod\.(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)",
            text,
        ):
            return text.rsplit(".", 1)[-1]
        return constants.get(text)
    if node.type == "binary_expression":
        left = _literal(node.child_by_field_name("left"), source, constants)
        right = _literal(node.child_by_field_name("right"), source, constants)
        operator = node.child_by_field_name("operator")
        if isinstance(left, str) and isinstance(right, str) and _text(operator, source) == "+":
            return left + right
    return None


def _values(value: Any) -> list[str] | None:
    values = value if isinstance(value, list) else [value]
    return values if all(isinstance(v, str) for v in values) else None


@dataclass
class AnalysisResult:
    entities: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    supported_paths: set[str] = field(default_factory=set)


@dataclass
class JavaClass:
    path: str
    service: str
    package: str
    qualified: str
    node: Node
    source: bytes
    imports: dict[str, str]
    wildcard_imports: set[str]
    fields: dict[str, str]
    constants: dict[str, Any]
    interfaces: list[str]
    methods: list[JavaMethod] = field(default_factory=list)
    key: str = ""


@dataclass
class JavaMethod:
    owner: JavaClass
    name: str
    parameter_types: list[str]
    variables: dict[str, str]
    node: Node
    key: str
    varargs: bool = False


class FileMetrics(dict):
    def __init__(self, analyzer, values):
        super().__init__(values)
        self.analyzer = analyzer

    def __setitem__(self, key, value):
        before = self.get(key, 0)
        super().__setitem__(key, value)
        path = self.analyzer.current_path
        if path:
            counters = self.analyzer.file_metrics.setdefault(path, {})
            counters[key] = counters.get(key, 0) + value - before


class Analyzer:
    def __init__(self, artifacts: list[dict], sources: dict[str, dict], texts: dict[str, str] | None = None):
        self.artifacts = {a["path"]: a for a in artifacts if a["kind"] == "source"}
        self.sources = sources
        self.texts = texts or {}
        self.result = AnalysisResult()
        self.classes: list[JavaClass] = []
        self.entity_by_key: dict[str, dict] = {}
        self.evidence_by_key: dict[str, dict] = {}
        self.relation_keys: set[tuple] = set()
        self.classes_by_identity: dict[tuple[str, str], list[JavaClass]] = {}
        self.current_path = None
        self.file_metrics = {}
        self.entity_owners = {}
        self.evidence_owners = {}
        self.relation_owners = {}
        self.metrics = FileMetrics(
            self,
            {
                "java_files": 0,
                "xml_files": 0,
                "parsed_files": 0,
                "methods": 0,
                "calls": 0,
                "resolved_calls": 0,
                "unresolved_calls": 0,
                "syntax_parsed_files": 0,
                "syntax_reused_files": 0,
            },
        )

    def issue(self, code: str, path: str, message: str, state="unsupported", **details):
        self.result.issues.append(
            {
                "code": code,
                "severity": "warning",
                "artifact_path": path,
                "message": message,
                "resolution_state": state,
                **details,
            }
        )

    def text(self, artifact: dict) -> str:
        if artifact["path"] in self.texts:
            return self.texts[artifact["path"]]
        return artifact["content"].decode(artifact.get("encoding") or "utf-8-sig")

    def entity(self, key: str, kind: str, name: str, content: str, path: str, locator: dict, metadata: dict):
        if key in self.entity_by_key:
            return self.entity_by_key[key]
        entity = {
            "stable_key": key,
            "type": kind,
            "display_name": name,
            "content": content,
            "artifact_path": path,
            "locator": locator,
            "metadata": {
                "parser_version": VERSION,
                "evidence_kind": "static",
                "runtime_source_alignment": "unknown",
                **metadata,
            },
        }
        self.entity_by_key[key] = entity
        self.entity_owners[key] = self.current_path or path
        self.result.entities.append(entity)
        if len(self.result.entities) > MAX_DECLARATIONS:
            raise ValueError("Java declaration limit exceeded")
        return entity

    def evidence(self, path: str, locator: dict, content: str, claim: str) -> str:
        artifact = self.artifacts[path]
        key = stable_key("evidence", path, locator, artifact["sha256"], claim)
        if key not in self.evidence_by_key:
            value = {
                "stable_key": key,
                "kind": "static",
                "artifact_path": path,
                "locator": dict(locator),
                "sha256": artifact["sha256"],
                "claim": claim,
                "content": content,
                "extractor_version": VERSION,
            }
            self.evidence_by_key[key] = value
            self.evidence_owners[key] = self.current_path or path
            self.result.evidence.append(value)
        return key

    def edge(self, a: str, b: str, kind: str, evidence: list[str], reason: str):
        identity = (a, b, kind)
        if identity in self.relation_keys:
            return
        self.relation_keys.add(identity)
        self.relation_owners[identity] = self.current_path
        self.result.relations.append(
            {
                "from_key": a,
                "to_key": b,
                "type": kind,
                "evidence_keys": evidence,
                "reason": reason,
                "causal_basis": "static_potential" if kind in ("may_call", "handler_of") else "not_established",
            }
        )

    def java(self, artifact: dict):
        self.current_path = artifact["path"]
        self.metrics["java_files"] += 1
        path = artifact["path"]
        text = self.text(artifact)
        source = text.encode("utf-8")
        if len(source) > MAX_SOURCE_BYTES:
            self.issue("VKS_JAVA_LIMIT", path, "单个 Java 文件超过静态解析范围")
            return
        root, reused = unit(
            "java", source, lambda: Parser(JAVA_LANGUAGE).parse(source).root_node, encode_java, decode_java
        )
        self.metrics["syntax_reused_files" if reused else "syntax_parsed_files"] += 1
        if root.has_error:
            self.issue("VKS_JAVA_SYNTAX", path, "Java 语法不完整；保留原始文件但不猜测声明")
            return
        package = ""
        imports, wildcard = {}, set()
        for child in root.named_children:
            if child.type == "package_declaration":
                package = _text(child, source).removeprefix("package").rstrip(";").strip()
            elif child.type == "import_declaration":
                imported = _text(child, source).removeprefix("import").rstrip(";").strip()
                if imported.startswith("static "):
                    continue
                if imported.endswith(".*"):
                    wildcard.add(imported[:-2])
                else:
                    imports[imported.rsplit(".", 1)[-1]] = imported
        service = self.sources[artifact["source_id"]].get("service_key", artifact["source_id"])

        def parse_class(node: Node, parent=""):
            name = _text(node.child_by_field_name("name"), source)
            qualified = ".".join(p for p in (parent or package, name) if p)
            body = node.child_by_field_name("body")
            if not body:
                return
            fields, constants, interfaces = {}, {}, []
            for member in body.named_children:
                if member.type == "field_declaration":
                    type_name = _text(member.child_by_field_name("type"), source)
                    for variable in member.named_children:
                        if variable.type != "variable_declarator":
                            continue
                        variable_name = _text(variable.child_by_field_name("name"), source)
                        fields[variable_name] = type_name
                        if all(
                            re.search(
                                r"\b" + modifier + r"\b",
                                _text(next((c for c in member.named_children if c.type == "modifiers"), None), source),
                            )
                            for modifier in ("static", "final")
                        ):
                            value = _literal(variable.child_by_field_name("value"), source, constants)
                            if value is not None:
                                constants[variable_name] = value
                                constants[name + "." + variable_name] = value
            for member in node.named_children:
                if member.type in ("super_interfaces", "extends_interfaces", "superclass"):
                    for item in member.named_children:
                        interfaces.extend(
                            _text(c, source) for c in item.named_children
                        ) if item.type == "type_list" else interfaces.append(_text(item, source))
            cls = JavaClass(
                path,
                service,
                package,
                qualified,
                node,
                source,
                imports,
                wildcard,
                fields,
                constants,
                interfaces,
                key=stable_key("symbol", service, qualified, "class"),
            )
            self.classes.append(cls)
            self.entity(
                cls.key,
                "symbol",
                qualified,
                _text(node, source),
                path,
                _span(node),
                {
                    "service_key": service,
                    "qualified_name": qualified,
                    "language": "java",
                    "kind": "interface" if node.type == "interface_declaration" else "class",
                    "resolution_state": "resolved",
                    "interfaces": interfaces,
                },
            )
            for member in body.named_children:
                if member.type in (
                    "class_declaration",
                    "interface_declaration",
                    "enum_declaration",
                    "record_declaration",
                ):
                    parse_class(member, qualified)
                if member.type not in ("method_declaration", "constructor_declaration"):
                    continue
                method_name = _text(member.child_by_field_name("name"), source)
                variables = dict(fields)
                parameter_types, varargs = [], False
                parameters = member.child_by_field_name("parameters")
                for param in parameters.named_children if parameters else []:
                    if param.type not in ("formal_parameter", "spread_parameter", "receiver_parameter"):
                        continue
                    type_node = param.child_by_field_name("type")
                    if param.type == "spread_parameter":
                        varargs = True
                        type_node = next(
                            (c for c in param.named_children if c.type not in ("modifiers", "variable_declarator")),
                            None,
                        )
                        variable = next((c for c in param.named_children if c.type == "variable_declarator"), None)
                        param_name = _text(variable.child_by_field_name("name") if variable else None, source)
                    else:
                        param_name = _text(param.child_by_field_name("name"), source)
                    type_name = (
                        _text(type_node, source)
                        + "".join(_text(c, source) for c in param.named_children if c.type == "dimensions")
                        + ("..." if param.type == "spread_parameter" else "")
                    )
                    parameter_types.append(type_name)
                    if param_name:
                        variables[param_name] = type_name
                signature = (
                    ("<init>" if member.type == "constructor_declaration" else method_name)
                    + "("
                    + ",".join(parameter_types)
                    + ")"
                )
                qualified_method = qualified + "." + method_name
                key = stable_key("symbol", service, qualified, signature)
                method = JavaMethod(cls, method_name, parameter_types, variables, member, key, varargs)
                cls.methods.append(method)
                self.metrics["methods"] += 1
                self.entity(
                    key,
                    "symbol",
                    qualified_method + "(" + ",".join(parameter_types) + ")",
                    _text(member, source),
                    path,
                    _span(member),
                    {
                        "service_key": service,
                        "qualified_name": qualified_method,
                        "signature": signature,
                        "parameter_types": parameter_types,
                        "return_type": _text(member.child_by_field_name("type"), source),
                        "language": "java",
                        "kind": "constructor" if member.type == "constructor_declaration" else "method",
                        "annotations": [_text(a, source) for a in _annotations(member)],
                        "resolution_state": "resolved",
                        "declaring_type": qualified,
                    },
                )
                ev = self.evidence(path, _span(member), _text(member, source), "AST 验证的 Java 方法声明")
                self.edge(cls.key, key, "contains", [ev], "类或接口包含此固定版本方法声明")
            if any(
                n.type == "method_invocation"
                and _text(n.child_by_field_name("name"), source)
                in {"invoke", "forName", "getMethod", "getDeclaredMethod"}
                for n in _walk(node)
            ):
                self.issue("VKS_JAVA_REFLECTION", path, "存在反射调用；不补画反射运行目标", "dynamic")

        for node in root.named_children:
            if node.type in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
                parse_class(node)
        self.result.supported_paths.add(path)
        self.metrics["parsed_files"] += 1

    def annotation_name(self, node: Node, cls: JavaClass) -> str | None:
        raw = _text(node.child_by_field_name("name"), cls.source)
        qualified = cls.imports.get(raw, raw)
        if "." not in raw and "org.springframework.web.bind.annotation" in cls.wildcard_imports:
            qualified = "org.springframework.web.bind.annotation." + raw
        if (
            qualified.startswith("org.springframework.web.bind.annotation.")
            and qualified.rsplit(".", 1)[-1] in MAPPINGS
        ):
            return qualified.rsplit(".", 1)[-1]
        return None

    def mapping(self, node: Node, cls: JavaClass):
        annotations = [(a, self.annotation_name(a, cls)) for a in _annotations(node)]
        annotations = [(a, n) for a, n in annotations if n]
        if not annotations:
            return None
        if len(annotations) != 1:
            self.issue(
                "VKS_SPRING_AMBIGUOUS_MAPPING", cls.path, "同一声明含多个映射注解；不选择任意第一项", "ambiguous"
            )
            return {"invalid": True}
        annotation, name = annotations[0]
        values = {}
        arguments = annotation.child_by_field_name("arguments")
        for arg in arguments.named_children if arguments else []:
            if arg.type == "element_value_pair":
                children = arg.named_children
                values[_text(children[0], cls.source)] = _literal(children[-1], cls.source, cls.constants)
            else:
                values["value"] = _literal(arg, cls.source, cls.constants)
        allowed = {"value", "path", "method", "params", "headers", "consumes", "produces", "name"}
        if set(values) - allowed or any(v is None for k, v in values.items() if k != "name"):
            self.issue(
                "VKS_SPRING_DYNAMIC_MAPPING",
                cls.path,
                "映射使用无法静态确定的常量或属性",
                "dynamic",
                line=node.start_point.row + 1,
            )
            return {"invalid": True}
        paths = _values(values.get("path", values.get("value", "")))
        if "path" in values and "value" in values and values["path"] != values["value"]:
            paths = None
        if not paths or any("${" in p or "#{" in p or "#" in p for p in paths):
            self.issue("VKS_SPRING_DYNAMIC_MAPPING", cls.path, "映射路径存在歧义或运行期占位符", "dynamic")
            return {"invalid": True}
        methods = _values(values["method"]) if "method" in values else []
        if name != "RequestMapping":
            methods = [name.removesuffix("Mapping").upper()]
        if methods is None or set(methods) - set(HTTP_METHODS):
            self.issue("VKS_SPRING_INVALID_METHOD", cls.path, "映射 HTTP 方法无法解析", "unsupported")
            return {"invalid": True}
        conditions = {}
        for kind in ("params", "headers", "consumes", "produces"):
            if kind in values:
                items = _values(values[kind])
                if items is None:
                    return {"invalid": True}
                conditions[kind] = sorted(set(items))
        return {"paths": paths, "methods": methods, "conditions": conditions}

    def spring(self):
        api_by_identity: dict[tuple, list[dict]] = {}
        for cls in self.classes:
            self.current_path = cls.path
            checkpoint()
            feign = self.feign(cls)
            if feign and feign.get("invalid"):
                continue
            top = self.mapping(cls.node, cls) or {"paths": [""], "methods": [], "conditions": {}}
            if top.get("invalid"):
                continue
            for method in cls.methods:
                checkpoint()
                bottom = self.mapping(method.node, cls)
                if not bottom or bottom.get("invalid"):
                    continue
                conditions = {}
                for condition in ("params", "headers"):
                    values = top["conditions"].get(condition, []) + bottom["conditions"].get(condition, [])
                    if values:
                        conditions[condition] = sorted(set(values))
                # Spring method-level consumes/produces override class-level declarations.
                for condition in ("consumes", "produces"):
                    values = bottom["conditions"].get(condition, top["conditions"].get(condition, []))
                    if values:
                        conditions[condition] = values
                methods = sorted(set(top["methods"] + bottom["methods"])) or list(HTTP_METHODS)
                fingerprint = sha256(canonical(conditions)).hexdigest()
                for prefix in top["paths"]:
                    for suffix in bottom["paths"]:
                        joined = (
                            prefix
                            if not suffix
                            else suffix
                            if not prefix
                            else prefix.rstrip("/") + "/" + suffix.lstrip("/")
                        )
                        route = joined if joined.startswith("/") else "/" + joined
                        if feign and feign["path"]:
                            route = "/" + feign["path"].strip("/") + route
                        for http_method in methods:
                            api_service = feign["service"] if feign else cls.service
                            identity = (api_service, http_method, route, fingerprint)
                            key = stable_key("api", "feign" if feign else "spring", *identity)
                            api = self.entity(
                                key,
                                "api",
                                f"{http_method} {route}",
                                _text(method.node, cls.source),
                                cls.path,
                                _span(method.node),
                                {
                                    "service_key": api_service,
                                    "method": http_method,
                                    "normalized_route": route,
                                    "condition_fingerprint": fingerprint,
                                    "conditions": conditions,
                                    "operation_id": cls.qualified + "." + method.name,
                                    "resolution_state": "external"
                                    if feign
                                    else "resolved"
                                    if top["methods"] or bottom["methods"]
                                    else "ambiguous",
                                    "api_origin": "feign" if feign else "spring",
                                    "declared_wildcard_methods": not (top["methods"] or bottom["methods"]),
                                    "client_method_key" if feign else "handler_key": method.key,
                                },
                            )
                            key_field = "client_method_keys" if feign else "handler_keys"
                            keys = api["metadata"].setdefault(key_field, [])
                            if method.key not in keys:
                                keys.append(method.key)
                            declaration = {
                                "method_key": method.key,
                                "artifact_path": cls.path,
                                "locator": _span(method.node),
                            }
                            declarations = api["metadata"].setdefault("source_declarations", [])
                            if declaration not in declarations:
                                declarations.append(declaration)
                            if not feign:
                                declarations = api_by_identity.setdefault(identity, [])
                                if api not in declarations:
                                    declarations.append(api)
                            evidence = self.evidence(
                                cls.path,
                                _span(method.node),
                                _text(method.node, cls.source),
                                "AST 验证的 Feign 请求声明" if feign else "AST 验证的 Spring 映射声明",
                            )
                            self.edge(
                                method.key,
                                key,
                                "may_call" if feign else "handler_of",
                                [evidence],
                                "Feign 接口声明的目标服务及路径；未观察网络调用"
                                if feign
                                else "Spring 类级与方法级声明组合；仅为静态 handler 映射",
                            )
        for identity, apis in api_by_identity.items():
            if len(apis) > 1 or any(len(api["metadata"]["source_declarations"]) > 1 for api in apis):
                for api in apis:
                    api["metadata"]["resolution_state"] = "ambiguous"
                    api["metadata"]["conflicting_handlers"] = api["metadata"].get("handler_keys", [])
                self.issue(
                    "VKS_SPRING_HANDLER_CONFLICT",
                    apis[0]["artifact_path"],
                    "同一服务与路由条件对应多个处理器；分别保留来源",
                    "ambiguous",
                )

    def feign(self, cls: JavaClass):
        for annotation in _annotations(cls.node):
            name = _text(annotation.child_by_field_name("name"), cls.source)
            name = cls.imports.get(name, name)
            if name not in {
                "org.springframework.cloud.openfeign.FeignClient",
                "org.springframework.cloud.netflix.feign.FeignClient",
            }:
                continue
            args = annotation.child_by_field_name("arguments")
            values = {}
            for arg in args.named_children if args else []:
                if arg.type == "element_value_pair":
                    values[_text(arg.named_children[0], cls.source)] = _literal(
                        arg.named_children[-1], cls.source, cls.constants
                    )
                else:
                    values["value"] = _literal(arg, cls.source, cls.constants)
            service, path = values.get("name", values.get("value")), values.get("path", "")
            if (
                not isinstance(service, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]+", service)
                or not isinstance(path, str)
                or "${" in path
                or "#{" in path
            ):
                self.issue("VKS_FEIGN_DYNAMIC_TARGET", cls.path, "Feign 服务或上下文路径无法静态确定", "dynamic")
                return {"invalid": True}
            return {"service": service, "path": path}
        return None

    def resolve_class(self, name: str, cls: JavaClass) -> list[JavaClass]:
        name = re.sub(r"<.*>", "", name).removesuffix("[]").strip()
        qualified = cls.imports.get(name, name)
        local = self.classes_by_identity.get((cls.service, qualified), [])
        if local:
            return local
        local = self.classes_by_identity.get((cls.service, f"{cls.package}.{name}".strip(".")), [])
        if local:
            return local
        # Wildcard imports are considered only if an actual declaration is present.
        candidates = [
            c for p in cls.wildcard_imports for c in self.classes_by_identity.get((cls.service, p + "." + name), [])
        ]
        return candidates

    def calls(self):
        for cls in self.classes:
            self.current_path = cls.path
            checkpoint()
            for interface_name in cls.interfaces:
                matches = self.resolve_class(interface_name, cls)
                if len(matches) == 1:
                    ev = self.evidence(
                        cls.path, _span(cls.node), _text(cls.node, cls.source), "AST 验证的继承或实现声明"
                    )
                    self.edge(
                        cls.key, matches[0].key, "implements", [ev], "固定源码中声明的继承或接口实现；不代表运行分派"
                    )
                else:
                    self.issue(
                        "VKS_JAVA_EXTERNAL_TYPE",
                        cls.path,
                        "继承或实现类型不在当前服务快照中唯一可解",
                        "external",
                        qualified_name=cls.qualified,
                    )
            for method in cls.methods:
                checkpoint()
                body = method.node.child_by_field_name("body")
                if body is None:
                    continue
                body_nodes = list(_walk(body))
                declarations = [n for n in body_nodes if n.type == "local_variable_declaration"]
                if len(body_nodes) > 100_000:
                    self.issue("VKS_JAVA_METHOD_LIMIT", cls.path, "方法语法节点超出有限静态调用分析范围", "unsupported")
                    continue
                for node in body_nodes:
                    checkpoint()
                    if node.type != "method_invocation":
                        continue
                    ancestor = node.parent
                    nested = False
                    while ancestor and ancestor != method.node:
                        if ancestor.type in (
                            "method_declaration",
                            "constructor_declaration",
                            "lambda_expression",
                            "class_body",
                        ):
                            nested = True
                            break
                        ancestor = ancestor.parent
                    if nested:
                        self.issue(
                            "VKS_JAVA_NESTED_CALL",
                            cls.path,
                            "嵌套类或 Lambda 的调用作用域尚未静态绑定",
                            "unsupported",
                            line=node.start_point.row + 1,
                        )
                        self.metrics["unresolved_calls"] += 1
                        self.metrics["calls"] += 1
                        continue
                    variables = self.variables_at(method, node, declarations)
                    self.metrics["calls"] += 1
                    name = _text(node.child_by_field_name("name"), cls.source)
                    obj = _text(node.child_by_field_name("object"), cls.source)
                    args = node.child_by_field_name("arguments")
                    arguments = list(args.named_children) if args else []
                    if obj in ("", "this"):
                        owners = [cls]
                    else:
                        field = obj.removeprefix("this.")
                        target_type = (
                            cls.fields.get(field, obj) if obj.startswith("this.") else variables.get(field, obj)
                        )
                        owners = self.resolve_class(target_type, cls)
                    candidates = [
                        m
                        for c in owners
                        for m in c.methods
                        if m.node.type == "method_declaration"
                        and m.name == name
                        and (
                            len(m.parameter_types) == len(arguments)
                            or m.varargs
                            and len(arguments) >= len(m.parameter_types) - 1
                        )
                    ]
                    if len(candidates) > 1:
                        argument_types = [self.argument_type(a, variables, cls.source) for a in arguments]
                        narrowed = [
                            m
                            for m in candidates
                            if not m.varargs
                            and all(self.compatible(a, b) for a, b in zip(argument_types, m.parameter_types))
                        ]
                        candidates = narrowed if narrowed else candidates
                    if len(candidates) != 1:
                        self.metrics["unresolved_calls"] += 1
                        state = (
                            "ambiguous"
                            if len(candidates) > 1 or len(owners) > 1
                            else "dynamic"
                            if name in {"invoke", "forName", "getMethod", "getDeclaredMethod"}
                            else "external"
                        )
                        self.entity_by_key[method.key]["metadata"].setdefault("unresolved_calls", []).append(
                            {"name": name, "line": node.start_point.row + 1, "resolution_state": state}
                        )
                        self.issue(
                            "VKS_JAVA_UNRESOLVED_CALL",
                            cls.path,
                            "调用目标无法在当前静态支持范围内唯一确定",
                            state,
                            qualified_name=cls.qualified + "." + method.name,
                            line=node.start_point.row + 1,
                        )
                        continue
                    target = candidates[0]
                    self.metrics["resolved_calls"] += 1
                    ev = self.evidence(cls.path, _span(node), _text(node, cls.source), "AST 验证的调用表达式与声明目标")
                    tev = self.evidence(
                        target.owner.path,
                        _span(target.node),
                        _text(target.node, target.owner.source),
                        "调用候选的固定版本方法声明",
                    )
                    self.edge(
                        method.key,
                        target.key,
                        "may_call",
                        [ev, tev],
                        "静态潜在调用；未验证条件执行、注入实例或运行因果",
                    )

    @staticmethod
    def variables_at(method: JavaMethod, invocation: Node, declarations):
        """Use lexical declarations in scope before this invocation only."""
        variables = dict(method.variables)
        for declaration in declarations:
            if declaration.start_byte >= invocation.start_byte:
                continue
            scope = declaration.parent
            while scope and scope.type not in ("block", "for_statement", "enhanced_for_statement", "switch_block"):
                scope = scope.parent
            if not scope or not scope.start_byte <= invocation.start_byte < scope.end_byte:
                continue
            type_name = _text(declaration.child_by_field_name("type"), method.owner.source)
            for variable in declaration.named_children:
                if variable.type == "variable_declarator":
                    variables[_text(variable.child_by_field_name("name"), method.owner.source)] = type_name
        return variables

    @staticmethod
    def argument_type(node: Node, variables: dict, source: bytes):
        if node.type == "identifier":
            return variables.get(_text(node, source))
        if node.type == "string_literal":
            return "String"
        if node.type in ("decimal_integer_literal", "hex_integer_literal"):
            return "long" if _text(node, source).lower().endswith("l") else "int"
        if node.type in ("decimal_floating_point_literal", "hex_floating_point_literal"):
            return "float" if _text(node, source).lower().endswith("f") else "double"
        if node.type in ("true", "false"):
            return "boolean"
        if node.type in ("object_creation_expression", "cast_expression"):
            return _text(node.child_by_field_name("type"), source)
        return None

    @staticmethod
    def compatible(argument: str | None, parameter: str):
        if argument is None:
            return True
        return argument.removeprefix("java.lang.") == parameter.removeprefix("java.lang.")

    def mybatis(self):
        documents = []
        fragments: dict[tuple, list] = {}
        for path, artifact in self.artifacts.items():
            self.current_path = path
            checkpoint()
            if not path.lower().endswith(".xml"):
                continue
            self.metrics["xml_files"] += 1
            text = self.text(artifact)
            if len(text.encode()) > MAX_SOURCE_BYTES:
                self.issue("VKS_MYBATIS_LIMIT", path, "单个 XML 文件超过解析范围")
                continue
            try:
                (root, spans), reused = unit(
                    "xml",
                    text.encode(),
                    lambda: (
                        SafeXML.fromstring(text, forbid_dtd=False, forbid_entities=True, forbid_external=True),
                        self.xml_spans(text),
                    ),
                    encode_xml,
                    decode_xml,
                )
                self.metrics["syntax_reused_files" if reused else "syntax_parsed_files"] += 1
            except (SafeXML.ParseError, DefusedXmlException):
                self.issue("VKS_MYBATIS_UNSAFE_XML", path, "XML 含不安全实体或语法错误；不解析其内容")
                continue
            if root.tag != "mapper":
                continue
            if any(depth > MAX_XML_DEPTH for _, depth in self.xml_walk(root)):
                self.issue("VKS_MYBATIS_LIMIT", path, "XML 嵌套超过解析范围")
                continue
            namespace = root.attrib.get("namespace", "")
            if not re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+", namespace):
                self.issue("VKS_MYBATIS_NAMESPACE", path, "Mapper namespace 缺失或无效")
                continue
            service = self.sources[artifact["source_id"]].get("service_key", artifact["source_id"])
            documents.append((path, service, namespace, root, text, spans))
            for fragment in root.findall("sql"):
                fragment_id = fragment.attrib.get("id", "")
                if fragment_id:
                    fragments.setdefault((service, namespace + "." + fragment_id), []).append(fragment)
            self.result.supported_paths.add(path)
            self.metrics["parsed_files"] += 1
        statement_counts = {}
        for _, service, namespace, root, _, _ in documents:
            checkpoint()
            for statement in root:
                if statement.tag in ("select", "insert", "update", "delete"):
                    identity = (service, namespace, statement.attrib.get("id"))
                    statement_counts[identity] = statement_counts.get(identity, 0) + 1
        for path, service, namespace, root, text, spans in documents:
            self.current_path = path
            for statement in root:
                if statement.tag not in ("select", "insert", "update", "delete"):
                    continue
                statement_id = statement.attrib.get("id")
                if not statement_id:
                    self.issue("VKS_MYBATIS_STATEMENT", path, "Mapper 语句缺少 id")
                    continue
                identity = (service, namespace, statement_id)
                duplicate = statement_counts[identity] > 1
                try:
                    sql, dynamic, includes = self.expand_sql(statement, service, namespace, fragments, set())
                except ValueError:
                    sql, dynamic, includes = " ".join(statement.itertext()), True, []
                    self.issue("VKS_MYBATIS_INCLUDE", path, "SQL include 缺失、多义或循环；不猜测展开结果", "ambiguous")
                rendered = " ".join(sql.split())
                substitution = "${" in rendered
                state = "ambiguous" if duplicate else "dynamic" if dynamic or substitution else "resolved"
                key = (
                    stable_key("sql", service, namespace, statement_id, path)
                    if duplicate
                    else stable_key("sql", service, namespace, statement_id)
                )
                span = spans.get((statement.tag, statement_id), [(1, len(text.splitlines()))]).pop(0)
                loc = {
                    "kind": "file_lines",
                    "startLine": span[0],
                    "endLine": span[1],
                    "qualifiedName": namespace + "." + statement_id,
                }
                entity = self.entity(
                    key,
                    "sql",
                    namespace + "." + statement_id,
                    rendered,
                    path,
                    loc,
                    {
                        "service_key": service,
                        "qualified_name": namespace + "." + statement_id,
                        "statement_kind": statement.tag,
                        "resolution_state": state,
                        "dynamic_tags": sorted(
                            {
                                e.tag
                                for e in statement.iter()
                                if e.tag
                                in {"if", "choose", "when", "otherwise", "foreach", "where", "set", "trim", "bind"}
                            }
                        ),
                        "include_refs": includes,
                        "result_map": statement.attrib.get("resultMap"),
                        "result_maps": {
                            e.attrib.get("id", ""): {
                                "attributes": dict(e.attrib),
                                "mappings": [
                                    {"tag": child.tag, **child.attrib} for child in e.iter() if child is not e
                                ],
                            }
                            for e in root.findall("resultMap")
                        },
                    },
                )
                ev = self.evidence(
                    path,
                    loc,
                    SafeXML.tostring(statement, encoding="unicode"),
                    "解析的 MyBatis namespace/id 与 SQL 结构",
                )
                methods = [
                    m
                    for c in self.classes
                    if c.service == service and c.qualified == namespace
                    for m in c.methods
                    if m.name == statement_id
                ]
                if len(methods) == 1 and not duplicate:
                    method = methods[0]
                    mev = self.evidence(
                        method.owner.path,
                        _span(method.node),
                        _text(method.node, method.owner.source),
                        "Mapper 接口方法声明",
                    )
                    self.edge(
                        method.key,
                        key,
                        "maps_to_sql",
                        [mev, ev],
                        "完整 namespace + statement id 映射；动态 SQL 仅保留结构",
                    )
                else:
                    self.issue(
                        "VKS_MYBATIS_METHOD",
                        path,
                        "Mapper 方法缺失、重载或语句重复；不自动选择目标",
                        "ambiguous" if methods or duplicate else "external",
                    )
                if state != "resolved":
                    self.issue("VKS_MYBATIS_DYNAMIC_SQL", path, "动态或歧义 SQL 不产生确定表读写关系", state)
                    continue
                # Only a small literal statement grammar is supported for table facts.
                # Joins/subqueries/vendor syntax remain explicit gaps, never fuzzy matches.
                operation = statement.tag
                pattern = {
                    "select": r"^SELECT\s+.+?\s+FROM\s+([A-Za-z_][\w.]*)\b",
                    "insert": r"^INSERT\s+INTO\s+([A-Za-z_][\w.]*)\b",
                    "update": r"^UPDATE\s+([A-Za-z_][\w.]*)\b",
                    "delete": r"^DELETE\s+FROM\s+([A-Za-z_][\w.]*)\b",
                }[operation]
                sql_code = self.sql_code(rendered)
                match = re.match(pattern, sql_code, re.IGNORECASE)
                if not match or re.search(
                    r"\b(?:JOIN|UNION|WITH)\b|\(\s*SELECT\b|\bFROM\b[^;]*,", sql_code, re.IGNORECASE
                ):
                    entity["metadata"]["table_resolution_state"] = "unsupported"
                    self.issue("VKS_MYBATIS_TABLE_UNSUPPORTED", path, "SQL 语法超出单表静态识别范围，未生成表关系")
                    continue
                table = match.group(1)
                table_key = stable_key("table", service, table.casefold())
                self.entity(
                    table_key,
                    "table",
                    table,
                    table,
                    path,
                    loc,
                    {"service_key": service, "table_name": table, "resolution_state": "resolved"},
                )
                self.edge(
                    key,
                    table_key,
                    "reads_table" if operation == "select" else "writes_table",
                    [ev],
                    "单表字面 SQL 结构；不代表数据库实际执行",
                )

    @staticmethod
    def xml_spans(text):
        parser = expat.ParserCreate()
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.ExternalEntityRefHandler = lambda *args: 0
        spans, stack = {}, []

        def start(tag, attributes):
            stack.append((tag, attributes.get("id"), parser.CurrentLineNumber))

        def end(tag):
            name, identifier, line = stack.pop()
            if identifier:
                spans.setdefault((name, identifier), []).append((line, parser.CurrentLineNumber))

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.Parse(text, True)
        return spans

    @staticmethod
    def sql_code(text):
        # SQL string literals and comments cannot create FROM/JOIN tokens.
        # Quoted identifiers are outside this intentionally small grammar.
        text = re.sub(r"'(?:''|[^'])*'", " 'VALUE' ", text)
        text = re.sub(r"/\*.*?\*/|--[^\r\n]*", " ", text, flags=re.DOTALL)
        if re.search(r'[`"]', text):
            return ""
        return " ".join(text.split())

    @staticmethod
    def xml_walk(root):
        stack = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            yield node, depth
            stack.extend((child, depth + 1) for child in node)

    def expand_sql(self, node, service, namespace, fragments, seen):
        parts = [node.text or ""]
        dynamic = node.tag in {"if", "choose", "when", "otherwise", "foreach", "where", "set", "trim", "bind"}
        refs = []
        if len(seen) > 16:
            raise ValueError("Include depth exceeded")
        for child in node:
            if child.tag == "include":
                ref = child.attrib.get("refid", "")
                qualified = ref if "." in ref else namespace + "." + ref
                if "${" in qualified or qualified in seen:
                    raise ValueError("Dynamic or cyclic include")
                matches = fragments.get((service, qualified), [])
                if len(matches) != 1 or list(child):
                    raise ValueError("Missing, ambiguous or parameterized include")
                value, nested_dynamic, nested_refs = self.expand_sql(
                    matches[0], service, qualified.rsplit(".", 1)[0], fragments, {*seen, qualified}
                )
                refs.extend([qualified, *nested_refs])
            else:
                value, nested_dynamic, nested_refs = self.expand_sql(child, service, namespace, fragments, seen)
                refs.extend(nested_refs)
            parts.append(value)
            parts.append(child.tail or "")
            dynamic |= nested_dynamic
        return " ".join(parts), dynamic, refs

    def run(self):
        for artifact in self.artifacts.values():
            checkpoint()
            if artifact["path"].lower().endswith(".java"):
                self.java(artifact)
        for cls in self.classes:
            checkpoint()
            self.classes_by_identity.setdefault((cls.service, cls.qualified), []).append(cls)
        self.spring()
        self.calls()
        from .async_topology import analyze_async

        analyze_async(self)
        self.mybatis()
        self.result.metrics = dict(
            self.metrics,
            entity_count=len(self.result.entities),
            relation_count=len(self.result.relations),
            unsupported_or_unresolved=len(self.result.issues),
            parser_version=VERSION,
            dependency_recomputed_files=len(self.result.supported_paths),
        )
        return self.result


def analyze_sources(
    artifacts: list[dict], sources: list[dict] | dict[str, dict], texts: dict[str, str] | None = None
) -> AnalysisResult:
    """Analyze authorized in-memory source bytes; never reads the source filesystem."""
    source_map = sources if isinstance(sources, dict) else {s["source_id"]: s for s in sources}
    from .incremental import analyze_incremental

    return analyze_incremental(artifacts, source_map, texts)
