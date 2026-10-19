import type { RulesPreset, Skill, SkillMode, Tracking, TrackingProfile } from './types';

/** AmaVolley mixed league 2025/26 (Trento). Article numbers refer to the rulebook. */
export const AMAVOLLEY: RulesPreset = {
  name: 'AmaVolley misto 2025/26',
  setPoints: 21, // art. 27
  setCap: 23,
  tiebreakPoints: 13,
  tiebreakCap: 16,
  winBy: 2,
  bestOf: 5, // art. 29
  timeoutsPerSet: 2, // art. 28
  subsPerSet: 8, // league raised art. 23's 6 to 8
  genderParity: { F: 3, M: 3 }, // art. 19
  maxLiberos: 2, // art. 22
  // art. 29; 3-1 / 1-3 are not listed, FIPAV values assumed.
  leaguePoints: { '3-0': 3, '3-1': 3, '3-2': 2, '2-3': 1, '1-3': 0, '0-3': 0 },
};

export const FIVB: RulesPreset = {
  name: 'FIVB',
  setPoints: 25,
  setCap: null,
  tiebreakPoints: 15,
  tiebreakCap: null,
  winBy: 2,
  bestOf: 5,
  timeoutsPerSet: 2,
  subsPerSet: 6,
  genderParity: null,
  maxLiberos: 2,
  leaguePoints: { '3-0': 3, '3-1': 3, '3-2': 2, '2-3': 1, '1-3': 0, '0-3': 0 },
};

export const PRESETS: RulesPreset[] = [AMAVOLLEY, FIVB];

const PROFILE_SKILLS: Record<Exclude<TrackingProfile, 'custom'>, Record<Skill, SkillMode>> = {
  score: { S: 'off', R: 'off', A: 'off', B: 'off', D: 'off', E: 'off', F: 'off' },
  basic: { S: 'all', R: 'errors', A: 'all', B: 'errors', D: 'errors', E: 'errors', F: 'errors' },
  full: { S: 'all', R: 'all', A: 'all', B: 'all', D: 'all', E: 'errors', F: 'errors' },
};

export const PROFILE_LABELS: Record<TrackingProfile, string> = {
  score: 'Score only',
  basic: 'Basic',
  full: 'Full',
  custom: 'Custom',
};

export function trackingForProfile(profile: TrackingProfile, base?: Tracking): Tracking {
  const skills = profile === 'custom' ? { ...(base?.skills ?? PROFILE_SKILLS.full) } : { ...PROFILE_SKILLS[profile] };
  return {
    profile,
    skills,
    recordEverySet: base?.recordEverySet ?? false,
    inferAssists: base?.inferAssists ?? true,
    ruleChecks: base?.ruleChecks ?? true,
  };
}

export const SKILL_NAMES: Record<Skill, string> = {
  S: 'Serve',
  R: 'Reception',
  A: 'Attack',
  B: 'Block',
  D: 'Dig',
  E: 'Set',
  F: 'Free ball',
};
