// Presentation of recorded material-metal simulation evidence, never chemistry.
export function materialMetalSummary(sample, events = []) {
  const prep = sample?.props?.material_metal_preparation;
  if (prep?.scope !== 'device_simulation') return null;
  const observation = sample.props.material_metal_observation;
  const event = [...events].reverse().find(e => e.kind === 'material_metal.simulation_accepted'
    && e.payload?.project_id === prep.project_id && e.payload?.chip_id === sample.id
    && e.payload?.evidence_job_id === observation?.job_id
    && e.payload?.scope === 'device_simulation' && e.payload?.simulation_accepted === true
    && e.payload?.experiment_accepted === false
    && e.payload?.goal?.preparation?.preparation_job_id === prep.preparation_job_id);
  const progress = Number.isFinite(observation?.qualitative_progress) && observation?.fully_visible
    ? observation.qualitative_progress : null;
  return {
    chipId: sample.id, layers: prep.layer_order || [], stampFilm: prep.stamp_film,
    progress, accepted: !!event, image: observation?.images?.at(-1) || prep.release_images?.at(-1),
    status: event ? 'Simulation target met' : observation ? 'Simulation target not accepted' : 'Prepared; awaiting Reactor',
    caption: 'Qualitative device simulation; real chemistry unverified',
    sourceIds: prep.top_first_flake_ids || [], releaseImages: prep.release_images || [],
  };
}
