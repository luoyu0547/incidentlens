import React, { useEffect, useState } from 'react';
import { Text } from 'ink';
import type { AgentActivityState } from '../state/cli-state.js';

export function ActivityLine({ activity }: { readonly activity: AgentActivityState }): React.ReactElement | null {
  const [, tick] = useState(0);

  useEffect(() => {
    if (activity.kind !== 'model') return;
    const timer = setInterval(() => tick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [activity.kind, activity.startedAt]);

  if (activity.kind !== 'model') return null;
  const started = activity.startedAt ? Date.parse(activity.startedAt) : Date.now();
  const elapsed = Number.isFinite(started) ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : 0;
  return (
    <Text color="yellow">
      ◌ 模型分析中 · 第 {activity.round ?? 1} 轮 · 已用时 {elapsed}s
    </Text>
  );
}
