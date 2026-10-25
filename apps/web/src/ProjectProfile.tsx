import "./configuration.css";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Save } from "lucide-react";
import { api, ApiError, mutation } from "./api";
import { Button, Notice, Panel, Spinner, Tag } from "./components";
import { usePermission, useWorkspace } from "./context";
import type { TeamMember } from "./types";

interface Profile {
  project_id: string;
  row_version: number;
  business_domain: string;
  responsible_user_id: string;
  responsible_name?: string;
  visibility: "project_members";
  default_analysis_language: "java" | "mixed" | "source_anchors";
}
const languages = {
  java: "Java / Spring / MyBatis",
  mixed: "混合资料",
  source_anchors: "人工源码锚点",
};
export function projectProfilePayload(profile: Profile) {
  return {
    business_domain: profile.business_domain.trim(),
    responsible_user_id: profile.responsible_user_id,
    visibility: "project_members" as const,
    default_analysis_language: profile.default_analysis_language,
  };
}
export function ProjectProfile() {
  const { w, notify, refresh } = useWorkspace();
  const canManage = usePermission("project:manage");
  const [profile, setProfile] = useState<Profile | null>(null),
    [members, setMembers] = useState<TeamMember[]>([]),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [value, rows] = await Promise.all([
        api<Profile>("/projects/" + w.project.id + "/profile"),
        canManage
          ? api<TeamMember[]>("/projects/" + w.project.id + "/members")
          : Promise.resolve([]),
      ]);
      setProfile(value);
      setMembers(rows);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [w.project.id, canManage]);
  useEffect(() => {
    void reload();
  }, [reload]);
  async function save(e: FormEvent) {
    e.preventDefault();
    if (!profile) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<Profile>(
        "/projects/" + w.project.id + "/profile",
        mutation("PUT", projectProfilePayload(profile), profile.row_version),
      );
      setProfile(result);
      notify("项目资料已保存，成员角色与实例所有权保持独立");
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "项目资料或成员配置已更新，当前输入保留。请复制变更后重新载入并核对。"
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  }
  const active = members.filter((member) => member.status === "active"),
    missingResponsible =
      !!profile?.responsible_user_id &&
      !active.some((member) => member.user_id === profile.responsible_user_id);
  return (
    <Panel
      className="project-profile-panel"
      title="项目资料"
      action={<Tag>项目成员可见</Tag>}
    >
      {error && (
        <Notice tone="red">
          <p>{error}</p>
          <Button
            kind="small"
            onClick={() => {
              void reload();
            }}
          >
            重新载入项目资料
          </Button>
        </Notice>
      )}
      {loading ? (
        <Spinner />
      ) : (
        profile &&
        (canManage ? (
          <form
            onSubmit={(e) => {
              void save(e);
            }}
          >
            <div className="configuration-fields">
              <label>
                业务领域
                <input
                  maxLength={160}
                  value={profile.business_domain}
                  onChange={(e) =>
                    setProfile({ ...profile, business_domain: e.target.value })
                  }
                  placeholder="例如：收费与开票"
                />
              </label>
              <label>
                项目责任人
                <select
                  required
                  value={profile.responsible_user_id || ""}
                  onChange={(e) =>
                    setProfile({
                      ...profile,
                      responsible_user_id: e.target.value,
                    })
                  }
                >
                  <option value="" disabled>
                    选择有效项目成员
                  </option>
                  {missingResponsible && (
                    <option value={profile.responsible_user_id || ""} disabled>
                      {profile.responsible_name || profile.responsible_user_id}
                      （已停用）
                    </option>
                  )}
                  {active.map((member) => (
                    <option key={member.user_id} value={member.user_id}>
                      {member.display_name || member.username || member.user_id}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                默认分析语言
                <select
                  value={profile.default_analysis_language}
                  onChange={(e) =>
                    setProfile({
                      ...profile,
                      default_analysis_language: e.target
                        .value as Profile["default_analysis_language"],
                    })
                  }
                >
                  {Object.entries(languages).map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
                <small>
                  用于记录项目资料的默认分析范围，导入仍按实际文件和支持集解析。
                </small>
              </label>
              <label>
                可见范围
                <input readOnly value="仅已授权项目成员" />
              </label>
            </div>
            <Notice>
              责任人从有效项目成员中指定；这个字段不授予角色、敏感权限或实例所有权。项目保持成员授权范围。
            </Notice>
            {missingResponsible && (
              <Notice tone="amber">
                当前责任人已停用，请选择有效成员后保存。
              </Notice>
            )}
            <div className="configuration-actions">
              <Button
                kind="primary"
                type="submit"
                disabled={
                  busy || missingResponsible || !profile.responsible_user_id
                }
              >
                <Save size={15} />
                保存项目资料
              </Button>
            </div>
          </form>
        ) : (
          <dl className="project-profile-read">
            <div>
              <dt>业务领域</dt>
              <dd>{profile.business_domain || "尚未填写"}</dd>
            </div>
            <div>
              <dt>项目责任人</dt>
              <dd>{profile.responsible_name || "暂未指定"}</dd>
            </div>
            <div>
              <dt>默认分析语言</dt>
              <dd>{languages[profile.default_analysis_language]}</dd>
            </div>
            <div>
              <dt>可见范围</dt>
              <dd>仅已授权项目成员</dd>
            </div>
          </dl>
        ))
      )}
    </Panel>
  );
}
