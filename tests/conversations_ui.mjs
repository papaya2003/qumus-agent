import assert from 'node:assert/strict';
import { test } from 'node:test';
import { conversationFacts } from '../qumus2/ui/static/conversations.js';
import { groupByConversation } from '../qumus2/ui/static/history.js';

test('a conversation row says who, when, how much and how far its plan is', () => {
  const now = 1_000_000;
  assert.equal(conversationFacts({ owner: 'Alice', last_active: now - 120, messages: 1, plan_steps: 4, plan_done: 1, cost_usd: 0.5 }, now),
    'by Alice · active 2 min ago · 1 message · plan 1/4 done · $0.50');
  assert.equal(conversationFacts({ messages: 0, plan_steps: 0, cost_usd: 0 }, now), '0 messages · no plan yet');
});

test('History groups earlier runs by conversation, most recent first', () => {
  const runs = [
    { id: 30, conversation: { id: 'b', name: 'B', owner: 'Bob' } },
    { id: 20, conversation: { id: 'a', name: 'A', owner: '' } },
    { id: 10, conversation: { id: 'b', name: 'B', owner: 'Bob' } },
    { id: 5 },
  ];
  const groups = groupByConversation(runs);
  assert.deepEqual(groups.map(g => [g.name, g.runs.map(r => r.id)]), [['B · Bob', [30, 10]], ['A', [20]], ['Earlier runs', [5]]]);
});
