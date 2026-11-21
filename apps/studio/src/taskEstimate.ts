type Progress = {
  startedAt: number;
  baseline: number;
  processed: number;
  total: number;
  at: number;
};

type Sample = Pick<Progress, "processed" | "at">;

export type TaskEstimate = {
  startedAt: number;
  baseline: number;
  total: number;
  samples: Sample[];
  pace?: number;
  calculatedAt?: number;
  finishAt?: number;
  calibrations?: number;
};

export type EstimateRange = { lower_seconds: number; upper_seconds: number };

const sampleWindow = 25_000;
const minimumSampleSpan = 18_000;
const updateInterval = 8_000;
const slowSampleWindow = 180_000;
const maxSamples = 96;

const staleAfter = (pace: number) => Math.max(30_000, pace * 2);

export function observeProgress(
  previous: TaskEstimate | null,
  { startedAt, baseline, processed, total, at }: Progress,
): TaskEstimate | null {
  if (!Number.isFinite(startedAt) || total <= 1 || processed <= baseline)
    return null;

  const sameRun =
    previous?.startedAt === startedAt &&
    previous.baseline === baseline &&
    previous.total === total &&
    previous.samples.at(-1)!.processed <= processed;
  const previousSample = sameRun ? previous.samples.at(-1) : undefined;
  // Once a measured run has clearly stalled, collect a fresh sample window
  // when it starts making progress again.
  const stalled =
    previous?.pace !== undefined &&
    previousSample &&
    at - previousSample.at >= staleAfter(previous.pace);
  const estimate =
    sameRun && !stalled
      ? previous
      : { startedAt, baseline, total, samples: [] };
  if (previousSample?.processed === processed) return previous;
  if (at < startedAt || (previousSample && at < previousSample.at))
    return estimate.samples.length ? estimate : null;

  // Keep actual completion samples. Time spent waiting for the first wave
  // does not describe the rate after the workers start delivering results.
  const samples = estimate.samples
    .filter((sample) => sample.at >= at - slowSampleWindow && sample.at < at)
    .slice(-(maxSamples - 1));
  samples.push({ processed, at });
  const recent = samples.find(
    (sample) =>
      at - sample.at <= sampleWindow &&
      at - sample.at >= minimumSampleSpan &&
      processed - sample.processed >= 4,
  );
  const first = recent ?? samples[0];
  const span = at - first.at;
  const completed = processed - first.processed;
  if (
    (!recent && (span < 30_000 || completed < 2 || samples.length < 3)) ||
    (estimate.calculatedAt !== undefined &&
      at - estimate.calculatedAt < updateInterval)
  ) {
    return { ...estimate, samples };
  }

  const recentPace = span / completed;
  const pace =
    estimate.pace === undefined
      ? recentPace
      : estimate.pace * 0.35 + recentPace * 0.65;
  return {
    ...estimate,
    samples,
    pace,
    calculatedAt: at,
    finishAt: at + (total - processed) * pace,
    calibrations: (estimate.calibrations ?? 0) + 1,
  };
}

export function estimatedSeconds(estimate: TaskEstimate | null, now: number) {
  if (estimate?.finishAt === undefined) return null;
  const last = estimate.samples.at(-1)!;
  if (now - last.at >= staleAfter(estimate.pace!) || now >= estimate.finishAt)
    return null;
  return (estimate.finishAt - now) / 1000;
}

export function roundRemaining(seconds: number, down = false) {
  const step = seconds < 60 ? 5 : seconds < 300 ? 15 : seconds < 900 ? 30 : 60;
  return (down ? Math.floor(seconds / step) : Math.ceil(seconds / step)) * step;
}

export function remainingRange(
  history: EstimateRange,
  elapsed: number,
  measured: number | null,
  calibrations: number,
) {
  const weight =
    measured === null
      ? 0
      : elapsed >= history.upper_seconds
        ? 1
        : Math.min(1, calibrations / 4);
  const blend = (boundary: number) =>
    Math.max(0, boundary - elapsed) * (1 - weight) + (measured ?? 0) * weight;
  return {
    lower: blend(history.lower_seconds),
    upper: blend(history.upper_seconds),
    source: weight === 0 ? "history" : weight === 1 ? "measured" : "blended",
  };
}
