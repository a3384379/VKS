# Markdown 图表与公式验收

本文件只包含合成内容，用于验证本地渲染。图表中的点击不会执行业务操作。

## Mermaid 流程图

```mermaid
flowchart LR
    A[导入文档] --> B{校验通过?}
    B -->|是| C[进入知识库]
    B -->|否| D[查看问题]
```

## Mermaid 时序图

```mermaid
sequenceDiagram
    participant U as 用户
    participant V as 知境
    U->>V: 打开文档
    V-->>U: 展示图表与公式
```

## PlantUML 时序图

```plantuml
@startuml
participant "用户" as User
participant "文档中心" as Docs
User -> Docs : 阅读知识
Docs --> User : 返回图表
@enduml
```

## PlantUML 类图

```puml
@startuml
class Document {
  title: String
  read()
}
class Chapter
Document *-- Chapter
@enduml
```

## 行内与块级公式

行内公式：$E = mc^2$，矩阵、分式和求和使用独立公式块。

$$
\sum_{i=1}^{n} i = \frac{n(n+1)}{2}
$$

```math
\begin{bmatrix} 1 & 2 \\ 3 & 4 \end{bmatrix}
```

## 错误语法也不影响正文

```mermaid
flowchart LR
A[unfinished
```

不合法的数学公式：$\frac{1}{$。

```plantuml
@startuml
Alice -> Bob : Hello
this is definitely not a command {
@enduml
```

这段正文在图表错误之后仍能阅读。
