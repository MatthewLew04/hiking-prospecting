"""geomodel — a Leapfrog-style 3-D geological modelling toolkit for the
NW Mineral Monitor.

The package is the reference implementation behind site/model3d.html (the
browser viewer/modeller ports the same algorithms and byte formats to JS).
It is organised as:

  model         in-memory objects (Grid2D, Mesh, LineSet, PointSet, BlockModel,
                Drillholes, ImagePlane, StratModel, Project) + JSON project I/O
  formats/      readers + writers for the interchange formats the mining /
                geophysics packages actually use (see GEOMODEL.md matrix):
                OMF v0.9 + v2.0, Surfer GRD, Geosoft GRD/GXF/XYZ, Arc ASCII,
                ZMAP+, Irap, UBC mesh/model, OBJ, DXF, GoCAD TS/PL, Leapfrog
                .msh, CSV tables (points / drillholes / structural / block
                models), SEG-Y, LAS
  interp        IDW, RBF (linear / thin-plate / spheroidal) and ordinary
                kriging with variogram models + empirical variograms
  stratigraphy  the "pancake" stacker: ordered contact surfaces -> units
  blockmodel    block-model estimation (kriging / IDW) + unit tagging
  slicing       plane sections: mesh-plane intersections, grid/block samples,
                marching-tetrahedra iso-surfaces for implicit (RBF) models
  workings      underground workings schema (adit/drift/shaft/raise/winze/
                stope) + 2-D-map-to-3-D georeferencing helpers
  kit           builds a model project for a mine or AOI from the repo bundles

and, on top of those, the path from a *written description* to a published
model — what services/minevis exposes to an agent as tool calls:

  narrative     USGS/USBM-style prose -> a typed WorkingsSpec plus the
                questions the prose does not answer.  Deterministic, offline,
                and it never invents: a missing bearing is a gap, not a default
  resolve       mine name -> located, cited candidates out of the 3,369-mine
                grades bundle.  Returns candidates, never a pick
  agentbuild    spec + resolved mine -> a Project, using only the primitives in
                `workings`; this module decides *placement* and refuses to place
                what the text does not locate
  mapplate      the handoff for workings traced off a georeferenced scan — the
                only path to `surveyed` confidence
  assay         the grades a description quotes (keeping selected apart from
                average) and the vein attitude it states
  render2d      plan / longitudinal section / isometric as plain SVG, with the
                line style carrying the confidence: surveyed solid, described
                dashed, assumed dotted
  publish       content-addressed model + manifest.json audit trail, written
                through a Target (S3, or a directory)

Design rules (same as the rest of pipelines/): stdlib only — numpy is used
when present to speed the solvers but every path has a pure-Python fallback;
remote fetches are cached and degrade honestly; nothing is invented — every
object carries provenance.
"""

__version__ = '1.0.0'
SCHEMA = 'nwmm-geomodel/1'
