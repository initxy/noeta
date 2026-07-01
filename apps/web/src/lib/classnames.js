function cn(...values) {
  return values
    .flatMap((value) => {
      if (!value) return [];
      if (Array.isArray(value)) return value;
      if (typeof value === "object") {
        return Object.entries(value)
          .filter(([, enabled]) => Boolean(enabled))
          .map(([name]) => name);
      }
      return [value];
    })
    .filter(Boolean)
    .join(" ");
}

export { cn };
