function mergeEvents(existing, incoming, taskId) {
  const bySeq = new Map();
  const noSeq = [];
  for (const env of existing.concat(incoming || [])) {
    if (!env || env.task_id !== taskId) continue;
    if (typeof env.seq === "number") {
      bySeq.set(env.seq, env);
    } else {
      noSeq.push(env);
    }
  }
  return noSeq.concat(
    Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq),
  );
}

export { mergeEvents };
