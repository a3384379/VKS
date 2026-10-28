import { Surface as Panel } from "./design-system/Surface";
import { useState } from "react";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { api, mutation } from "./api";
import { Button, Notice, Tag } from "./components";
import { ReadScope, usePermission, useWorkspace } from "./context";
import { SourceManagement } from "./SourceManagement";
import { ImportCenter } from "./ImportCenter";
import { SourceDiagnostics } from "./SourceDiagnostics";
import { OnboardingReport } from "./OnboardingReport";
import { OnboardingFlowDraft } from "./OnboardingFlowDraft";

export function OnboardingWizard() {
  const { w, navigate } = useWorkspace();
  const canImport = usePermission("source:import");
  const [step, setStep] = useState(0),
    [goal, setGoal] = useState(""),
    [hasCode, setHasCode] = useState(true),
    [report, setReport] = useState<{
      issues: string[];
      preflight_sha256: string;
    } | null>(null),
    [error, setError] = useState("");
  const steps = [
    "范围与目标",
    "来源与版本",
    "隐私与约束",
    "预检与接入",
    "任务与成果",
  ];
  return (
    <>
      <Panel
        title="智能接入向导"
        action={<Tag>当前项目 · {w.project.name}</Tag>}
      >
        <ol className="v2-steps">
          {steps.map((title, i) => (
            <li key={title}>
              <button
                aria-current={i === step ? "step" : undefined}
                onClick={() => setStep(i)}
              >
                <b>{String(i + 1).padStart(2, "0")}</b>
                {title}
              </button>
            </li>
          ))}
        </ol>
        {error && <Notice tone="red">{error}</Notice>}
        {step === 0 ? (
          <div className="v2-wizard-body">
            <h2>这次要整理什么业务？</h2>
            <p>以一个可核对的业务域开始；资料将保留原始字节和独立来源版本。</p>
            <label>
              业务目标
              <textarea
                aria-label="业务目标"
                value={goal}
                maxLength={2000}
                onChange={(e) => setGoal(e.target.value)}
                placeholder="例如：整理批量开票的页面动作、校验条件及接口依据"
              />
            </label>
            <label>
              <input
                type="checkbox"
                checked={hasCode}
                onChange={(e) => setHasCode(e.target.checked)}
              />
              本次提供源码
            </label>
            {!hasCode && (
              <Notice>
                可以继续接入文档和原型；源码调用链、SQL
                定位和版本对齐将保留缺口。
              </Notice>
            )}
          </div>
        ) : step === 1 ? (
          <div className="v2-wizard-body">
            <h2>固定每一种来源的版本</h2>
            <p>
              Git/SVN/目录采集在你明确授权的本机执行。此页面只登记来源；服务器不会读取本机路径。
            </p>
            <SourceManagement />
            <p>
              <a className="button" href="/api/v2/collector-agent" download>
                下载本机只读采集 Agent（Windows）
              </a>
            </p>
          </div>
        ) : step === 2 ? (
          <div className="v2-wizard-body">
            <ShieldCheck size={30} />
            <h2>资料出口与运行约束</h2>
            <p>
              接入不会启用平台模型。模型需要在项目设置中单独配置和授权，禁止出口的材料始终排除。
            </p>
            <Notice>
              普通业务探索只读已保存状态。受控采集另需登记 Test/Mock
              环境、合成数据和固定修订人员审批。
            </Notice>
            <Button
              disabled={!canImport}
              onClick={() => {
                setError("");
                void api<{ issues: string[]; preflight_sha256: string }>(
                  `/api/v2/projects/${w.project.id}/onboarding/preflight`,
                  mutation("POST", {
                    analysis_set_id: w.selected_analysis_set?.id,
                    goal,
                    source_code_available: hasCode,
                    allow_platform_llm: false,
                  }),
                )
                  .then((r) => {
                    setReport(r);
                    setStep(3);
                  })
                  .catch((e) => setError(e.message));
              }}
            >
              检查接入范围
            </Button>
          </div>
        ) : step === 3 ? (
          <div className="v2-wizard-body">
            <h2>检查问题并上传原始材料</h2>
            {report && (
              <Notice>
                {report.issues.length
                  ? report.issues.join("；")
                  : "当前接入范围检查完成，上传文件后继续执行内容安全预检。"}
                <small className="v2-hash">
                  预检摘要：{report.preflight_sha256}
                </small>
              </Notice>
            )}
            <p>
              下方上传执行真实的大小、编码、摘要和权限校验；预检成功后可创建解析任务。
            </p>
          </div>
        ) : (
          <div className="v2-wizard-body">
            <h2>查看解析任务，建立可审核知识</h2>
            <p>
              当前可见 {w.jobs.length} 个近期任务、{w.pages.length} 个页面、
              {w.actions.length} 个动作。候选与确认分别保留。
            </p>
            <div className="row wrap">
              <Button onClick={() => navigate("agent")}>进入知识任务</Button>
              <Button onClick={() => navigate("workbench")}>探索业务</Button>
              <Button onClick={() => navigate("reviews")}>审核候选</Button>
            </div>
            <OnboardingFlowDraft />
          </div>
        )}
        <div className="v2-wizard-actions">
          <Button disabled={step === 0} onClick={() => setStep((s) => s - 1)}>
            上一步
          </Button>
          <Button
            kind="primary"
            disabled={step === 4}
            onClick={() => setStep((s) => s + 1)}
          >
            下一步 <ArrowRight size={14} />
          </Button>
        </div>
      </Panel>
      {step === 3 && <ImportCenter />}
      <ReadScope>
        {(step === 1 || step === 4) && <SourceDiagnostics />}
        {step === 4 && <OnboardingReport />}
      </ReadScope>
    </>
  );
}
