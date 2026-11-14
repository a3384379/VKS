import {
  clampWindowBox,
  startFloatingGesture,
} from "../packages/assistant-runtime/src/floating.ts";
import { preferredAnswerModels } from "../apps/web/src/modelChoices.ts";

globalThis.VKSAssistant = Object.freeze({
  clampWindowBox,
  startFloatingGesture,
  preferredAnswerModels,
});
