export function buildMessagePayload(message) {
  const text = message.trim();
  if (!text) throw new Error('Enter a message.');
  if (text.length > 2000) throw new Error('Keep your message within 2,000 characters.');
  return { text };
}

export const items = value => Array.isArray(value) ? value : [];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
export function assessmentContent(payload) {
  if (!object(payload)) return null;
  const classification = object(payload.classification) ? payload.classification : null;
  const plan = object(payload.safety_plan) ? payload.safety_plan : null;
  const guidance = classification?.emergency_guidance;
  return {
    classification,
    alert: typeof payload.alert === 'string' ? payload.alert : '',
    steps: items(payload.calm_steps).length ? payload.calm_steps : items(plan?.steps).length ? plan.steps : items(guidance?.do_now),
    warnings: [...items(classification?.warnings), ...items(guidance?.avoid)],
    actions: items(classification?.recommended_actions),
    sources: items(classification?.sources),
    places: [plan?.best_place, ...items(plan?.backup_places)].filter(object),
    locationPrompt: typeof payload.location_prompt === 'string' ? payload.location_prompt : '',
  };
}
export function messageText(payload) {
  if (typeof payload === 'string') return payload;
  if (object(payload)) {
    for (const key of ['text', 'reply', 'answer', 'alert']) {
      if (typeof payload[key] === 'string' && payload[key].trim()) return payload[key];
    }
  }
  return JSON.stringify(payload, null, 2);
}
