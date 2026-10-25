# Java/Spring/MyBatis 合成回归基线

`v1/` 与 `v2/` 是静态分析输入，不编译或执行。billing/support 故意保留相同类名与路由，通过服务标识隔离。v2 改动提交接口路径、移动 Service 文件并改变 SQL 字段，供版本漂移测试使用。

`gold.json` 冻结 40 条合成关系标签及所有 v1 原始字节摘要。分析器从未读取标签；评测器在分析完成后比较。标签是工程合成回归用例，尚未经独立人工盲审，不能代替设计要求的 100 条真实关系真值。

```bash
uv run python -m packages.analyzers.evaluate_java
```

评测分别报告 TP/FP/FN、precision/recall、候选数量、确认数量、调用解析分母与覆盖率。预设的标准库返回类型传播是已知缺口：`String.valueOf(value)` 无法在当前快照解析为 String，因此不会猜测该调用的重载目标。反射和未提供的库声明也按 dynamic/external 报告，不隐藏失败样本。

支持范围：Tree-sitter Java 声明与重载、字面/有限常量 Spring 映射、服务隔离、参数/请求头/媒体类型条件、有限静态调用、Feign 声明、MyBatis XML namespace/id/include/resultMap、动态 SQL 结构、有限单表字面关系。树形语法不是完整 Java 编译器、运行追踪或通用 SQL 引擎。
