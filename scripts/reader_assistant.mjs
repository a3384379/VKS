import {
  clampWindowBox,
  startFloatingGesture,
} from "../packages/assistant-runtime/src/floating.ts";
import { preferredAnswerModels } from "../apps/web/src/modelChoices.ts";
import {
  DetachedAssistantWindow,
  DocumentAssistantFrame,
} from "../packages/assistant-runtime/src/detached.ts";

globalThis.VKSAssistant = Object.freeze({
  clampWindowBox,
  startFloatingGesture,
  preferredAnswerModels,
  DetachedAssistantWindow,
  DocumentAssistantFrame,
});
