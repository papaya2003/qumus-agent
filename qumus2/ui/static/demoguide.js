// Presentation guidance, separate from the immutable recorded evidence.
// Recordings named here get a readable History title and, when selected, a
// one-line purpose. Diagnostics open on their Evidence tab.
export const DIAGNOSTICS = {};

export const DEMOS = {
  // Third element true: listed in History (owner, 2026-11-06: only the latest Hall-bar run).
  'hallbar-live-sol-seed3-20261106-r1': ['hBN/Gr Hall bar, live gpt-6.1-sol, seed 3 (6 Nov)', 'Live model resumed from the seed-3 save point just before pair selection: it chose the hBN/graphene pair, laid out the device and stacked it on the eight-electrode chip.', true],
};

// The recordings History lists. Any other recording folder still plays by link
// (?rec=<name>) and is listed only while selected or playing.
export const CURRENT = new Set(Object.keys(DEMOS).filter(name => DEMOS[name][2]));
