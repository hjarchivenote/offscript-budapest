const calendar = new Intl.DateTimeFormat('en-GB', {
  timeZone:'Europe/Budapest', year:'numeric', month:'2-digit', day:'2-digit',
});

export function budapestDay(instant) {
  // Server timestamps must identify an instant; never assume the device's zone.
  if (typeof instant !== 'string' || !/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(instant)) return null;
  const date = new Date(instant);
  if (!Number.isFinite(date.getTime())) return null;
  const parts = Object.fromEntries(calendar.formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
