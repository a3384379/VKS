"""Literal Kafka/XXL declarations. No broker, scheduler or delivery is inferred.

Endpoint identities include service, declaring method and direction. Equal topic
names in different beans/services never prove a shared cluster or delivery path.
"""

import re
from .cancellation import checkpoint
from .java_spring import _annotations, _literal, _span, _text, _walk, stable_key

KAFKA_LISTENER = "org.springframework.kafka.annotation.KafkaListener"
XXL_JOB = "com.xxl.job.core.handler.annotation.XxlJob"
KAFKA_TYPES = {"org.springframework.kafka.core.KafkaTemplate", "org.springframework.kafka.core.KafkaOperations"}


def qualified(name, cls):
    bare = name.split("<", 1)[0].strip()
    return cls.imports.get(bare, bare)


def values(annotation, cls):
    arguments = annotation.child_by_field_name("arguments")
    result = {}
    for arg in arguments.named_children if arguments else []:
        if arg.type == "element_value_pair":
            result[_text(arg.named_children[0], cls.source)] = _literal(
                arg.named_children[-1], cls.source, cls.constants
            )
        else:
            result["value"] = _literal(arg, cls.source, cls.constants)
    return result


def literal(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,249}", value) and value not in (".", "..")


def analyze_async(analyzer):
    jobs = {}

    def endpoint(method, node, adapter, name, direction, **metadata):
        cls = method.owner
        key = stable_key("async", cls.service, method.key, adapter, direction, name)
        entity = analyzer.entity(
            key,
            "symbol",
            name,
            _text(node, cls.source),
            cls.path,
            _span(node),
            dict(
                service_key=cls.service,
                adapter=adapter,
                kind="message_endpoint" if adapter == "kafka" else "job_handler",
                direction=direction,
                resolution_state="declared",
                declaring_method_key=method.key,
                deployment_alignment="unknown",
                **metadata,
            ),
        )
        ev = analyzer.evidence(
            cls.path, _span(node), _text(node, cls.source), "AST 验证的消息/任务字面声明；未验证运行部署"
        )
        source, target = (method.key, key) if direction == "produce" else (key, method.key)
        analyzer.edge(source, target, "may_call", [ev], "固定来源静态潜在关联；不证明消息投递、调度执行或运行因果")
        return entity

    for cls in analyzer.classes:
        for method in cls.methods:
            checkpoint()
            for annotation in _annotations(method.node):
                name = qualified(_text(annotation.child_by_field_name("name"), cls.source), cls)
                if name not in (KAFKA_LISTENER, XXL_JOB):
                    continue
                args = values(annotation, cls)
                if name == XXL_JOB:
                    handler = args.get("value")
                    if not literal(handler):
                        analyzer.issue(
                            "VKS_XXL_DYNAMIC_HANDLER", cls.path, "XXL-Job handler 不是可确定的字面名称", "dynamic"
                        )
                        continue
                    entity = endpoint(method, annotation, "xxl_job", handler, "invoke", handler=handler)
                    jobs.setdefault((cls.service, handler), []).append(entity)
                    analyzer.issue(
                        "VKS_XXL_SCHEDULE_UNKNOWN",
                        cls.path,
                        "仅发现 XXL-Job handler；调度配置、条件与运行版本未提供",
                        "external",
                    )
                else:
                    topics = args.get("topics")
                    topics = topics if isinstance(topics, list) else [topics]
                    if (
                        not topics
                        or not all(literal(topic) for topic in topics)
                        or args.get("topicPattern") is not None
                        or "topicPartitions" in args
                    ):
                        analyzer.issue(
                            "VKS_KAFKA_DYNAMIC_TOPIC",
                            cls.path,
                            "Kafka topic 表达式/分区声明需运行配置，不猜测目标",
                            "dynamic",
                        )
                        continue
                    for topic in sorted(set(topics)):
                        endpoint(
                            method,
                            annotation,
                            "kafka",
                            topic,
                            "consume",
                            topic=topic,
                            group_id=args.get("groupId") if literal(args.get("groupId")) else None,
                        )
                    analyzer.issue(
                        "VKS_KAFKA_DEPLOYMENT_UNKNOWN",
                        cls.path,
                        "仅发现 Kafka 消费声明；集群、分区、消息匹配与投递因果未知",
                        "external",
                    )
            body = method.node.child_by_field_name("body")
            if body is None:
                continue
            nodes = list(_walk(body))
            declarations = [node for node in nodes if node.type == "local_variable_declaration"]
            for node in nodes:
                checkpoint()
                if node.type != "method_invocation":
                    continue
                ancestor = node.parent
                while (
                    ancestor is not None
                    and ancestor != method.node
                    and ancestor.type
                    not in ("lambda_expression", "class_body", "method_declaration", "constructor_declaration")
                ):
                    ancestor = ancestor.parent
                if ancestor != method.node:
                    continue
                obj = _text(node.child_by_field_name("object"), cls.source)
                variables = analyzer.variables_at(method, node, declarations)
                field = obj.removeprefix("this.")
                typ = cls.fields.get(field, "") if obj.startswith("this.") else variables.get(obj, "")
                if qualified(typ, cls) not in KAFKA_TYPES:
                    continue
                call = _text(node.child_by_field_name("name"), cls.source)
                if call not in ("send", "sendDefault"):
                    continue
                arguments = node.child_by_field_name("arguments")
                arguments = list(arguments.named_children) if arguments else []
                topic = (
                    _literal(arguments[0], cls.source, cls.constants)
                    if call == "send" and len(arguments) >= 2
                    else None
                )
                if not literal(topic):
                    analyzer.issue(
                        "VKS_KAFKA_DYNAMIC_TOPIC",
                        cls.path,
                        "Kafka 默认 topic/ProducerRecord/动态参数不能静态确定目标",
                        "dynamic",
                        line=node.start_point.row + 1,
                    )
                    continue
                endpoint(method, node, "kafka", topic, "produce", topic=topic)
                analyzer.issue(
                    "VKS_KAFKA_DEPLOYMENT_UNKNOWN",
                    cls.path,
                    "仅发现 Kafka 发送声明；未证明消息已发送或与某消费者同集群",
                    "external",
                )
    for (_, handler), entities in jobs.items():
        if len(entities) > 1:
            for entity in entities:
                entity["metadata"]["resolution_state"] = "ambiguous"
                analyzer.issue(
                    "VKS_XXL_HANDLER_CONFLICT",
                    entity["artifact_path"],
                    "同一服务多个方法声明相同 XXL handler；不选择任意目标",
                    "ambiguous",
                    handler=handler,
                )
