# GridBandhu Frontend Prototype

This is a standalone frontend-only simulation for the GridBandhu distribution-control prototype. It does not connect to PostgreSQL, Redis, FastAPI, or a WebSocket.

## Run locally

```bash
npm install
npm run dev
```

## Prototype behavior

- Topology, connections, road-routed coordinates, wire ratings, and telemetry are a local snapshot of the official 63-node/53-edge network in `src/data/topology.json`.
- Hover a connection on the map to inspect its route, wire size, rating, flow, voltage, and status.
- Use **SIMULATE FAULT** to create a feeder overload, highlight the affected path, and generate three dynamic MILP-style suggestions.
- Select a suggestion to preview a different path color.
- Suggestions are advisory only; there is deliberately no execution/apply button.
- Use **CLEAR SIMULATED FAULT** to return to the nominal state.

The production integration remains in the sibling `frontend` and `backend` directories. This folder can be copied into its own repository and deployed to Vercel as a Vite static frontend.
