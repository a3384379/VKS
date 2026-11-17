import { useEffect, useRef, useState } from "react";
export type DisplaySpeed = "normal" | "fast" | "instant";
export function displaySpeed(): DisplaySpeed {
  try {
    const value = localStorage.getItem("vks.ai.display-speed");
    return value === "fast" || value === "instant" ? value : "normal";
  } catch {
    return "normal";
  }
}
export function saveDisplaySpeed(speed: DisplaySpeed) {
  try {
    localStorage.setItem("vks.ai.display-speed", speed);
  } catch {
    /* memory only */
  }
  window.dispatchEvent(new Event("vks-ai-display"));
}
export function useDisplaySpeed() {
  const [speed, setSpeed] = useState(displaySpeed);
  useEffect(() => {
    const update = () => setSpeed(displaySpeed());
    window.addEventListener("vks-ai-display", update);
    return () => window.removeEventListener("vks-ai-display", update);
  }, []);
  return speed;
}
export function useDisplayedText(text: string) {
  const speed = useDisplaySpeed(),
    [shown, setShown] = useState("");
  const visible = useRef("");
  useEffect(() => {
    if (speed === "instant" || !text) {
      visible.current = text;
      setShown(text);
      return;
    }
    let timer = 0;
    const tick = () => {
      const base = text.startsWith(visible.current) ? visible.current : "";
      const next = Array.from(text)
        .slice(0, Array.from(base).length + (speed === "fast" ? 24 : 8))
        .join("");
      visible.current = next;
      setShown(next);
      if (next !== text) timer = requestAnimationFrame(tick);
    };
    timer = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(timer);
  }, [text, speed]);
  return shown;
}
