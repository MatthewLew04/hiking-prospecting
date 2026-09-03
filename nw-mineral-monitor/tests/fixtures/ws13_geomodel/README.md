# ws13_geomodel fixture

Eight ws13_documents-shaped rows (one per line of `documents.jsonl`, plus one
`ws13_mine_id_map` row marked `"_table"`) and their per-page text under
`pages/<sha256>/<page>.txt`, so `pipelines/ws13_geomodel.py` runs end to end
with no Postgres and no S3.  Every mine named exists in
`site/data/grades/grades.json` (Nevada), so the resolver's grades tier finds
it; the sha256 values are real digests (`sha256("ws13-geomodel-fixture:<label>")`)
so the shard arithmetic sees real entropy.

| label | what it proves |
|---|---|
| clean | one mine, a complete description: resolves, builds, publishes |
| district | three mines in one report: one section and one model per mine |
| novocab | no workings vocabulary: dropped by the lexical prefilter |
| ambiguous | two grades rows share the name: parked, never chosen |
| norights | licensed copy without a rights_basis: publish refused |
| unchanged | verified id-map row; run twice, the second run skips on the content hash |
| omit | a drift with no bearing: answered omit, model carries assumed = 0 |
| unknown | phrasing the grammar does not know: questions, no elements, nothing built |
