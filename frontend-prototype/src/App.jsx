import { useEffect, useMemo, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import './App.css'
import './map-overrides.css'
import './white-theme.css'
import './accent-theme.css'
import topologySnapshot from './data/topology.json'

const BASEMAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty'

const nodeById = Object.fromEntries(topologySnapshot.nodes.map((node) => [node.id, node]))
const feederNames = {
  '205': 'Hospital–Veena Nagar Feeder', '206': 'Market–Shiv Shakti Feeder',
  '207': 'Kisan Nagar North Feeder', '208': 'Model Town West Feeder',
  '209': 'Kailash Nagar Line', '210': 'East Link Line',
  '211': 'Manpada Ring Feeder', '212': 'Ambaji Dham Ring'
}
const feederLocations = {
  '205': { '001':'Hospital Gate', '002':'Veena Nagar Junction', '003':'Market Road Tee', '004':'Kisan Nagar Crossing', '005':'Central Loop End', '006':'Hospital Service Tap' },
  '206': { '006':'Market III Source', '007':'Shiv Shakti Junction', '008':'Ambaji Dham Tee', '009':'Kisan Nagar Society Tap', '010':'Manpada Crossing', '011':'Society End' },
  '207': { '011':'Kisan Nagar North Source', '012':'Model Town Junction', '013':'Wagle Circle Tee', '014':'Industrial Estate Tap', '015':'North Feeder End' },
  '208': { '015':'Model Town West Source', '016':'Ravi Fisheries Junction', '017':'Mulund Check Naka Tee', '018':'West Feeder End' },
  '209': { '018':'Kailash Nagar Source', '019':'Kailash Substation Tee', '020':'Ganesh Pada Junction', '021':'Kailash Line End' },
  '210': { '021':'East Link Source', '022':'Rayladevi Junction', '023':'Lake Road Tee', '024':'East Line End' },
  '211': { '025':'Manpada Ring Source', '026':'Manpada CHS Junction', '027':'Gautam Nagar Tee', '028':'Babu Jagjivan Nagar Tap', '029':'Ring Return' },
  '212': { '029':'Ambaji Dham Source', '030':'D C Das Marg Junction', '031':'Shiv Shakti Return', '032':'South Ring End' }
}
const feederForId = (id = '') => feederNames[id.match(/\/([0-9]+)\/P[0-9]+$/)?.[1]] || 'Distribution line'
const readableNodeName = (node) => {
  const pole = node.id.match(/\/([0-9]+)\/P([0-9]+)$/)
  if (!pole) return node.name || node.id
  const location = feederLocations[pole[1]]?.[pole[2]] || `Pole ${pole[2]}`
  return `${feederForId(node.id)} · ${location}`
}
const topologyNodes = topologySnapshot.nodes.map((node) => ({ ...node, name: readableNodeName(node), capacity_kw: node.capacity_kw || Math.max(node.load || 0, 200), telemetry: { current_load_kw: node.load || 0, voltage_pu: '1.000', loading_percentage: node.load ? Math.round((node.load / Math.max(node.capacity_kw || 200, 1)) * 100) : 0, status: node.status || 'healthy' } }))
const compactRoute = (coordinates) => coordinates.length < 16 ? coordinates : coordinates.filter((point, index) => index === 0 || index === coordinates.length - 1 || index % 3 === 0)
const topologyEdges = topologySnapshot.edges.map((edge) => ({ ...edge, coordinates: compactRoute(edge.coordinates), fromName: readableNodeName(nodeById[edge.from]), toName: readableNodeName(nodeById[edge.to]), displayName: `${readableNodeName(nodeById[edge.from])} → ${readableNodeName(nodeById[edge.to])}`, wire_size: `${edge.type === 'ht' ? 150 : 95} mm²`, rating_kw: edge.capacity, flow_kw: edge.flow, status: edge.status === 'healthy' ? 'energized' : edge.status, voltage_kv: edge.voltage }))
const demoFaultEdge = topologyEdges[6]
const demoStart = nodeById[demoFaultEdge.from]
const demoEnd = nodeById[demoFaultEdge.to]
const demoTieEdges = [
  { id:'RMU-TIE-A', from:demoFaultEdge.from, to:demoFaultEdge.to, type:'rmu-tie', displayName:'RMU Tie A · Market Loop bypass', fromName:readableNodeName(demoStart), toName:readableNodeName(demoEnd), coordinates:[[demoStart.lng,demoStart.lat],[demoStart.lng + .00042,demoStart.lat + .00016],[demoEnd.lng + .00038,demoEnd.lat + .00012],[demoEnd.lng,demoEnd.lat]], wire_size:'150 mm²', rating_kw:315, flow_kw:0, status:'normally open', voltage_kv:11 },
  { id:'RMU-TIE-B', from:demoFaultEdge.from, to:demoFaultEdge.to, type:'rmu-tie', displayName:'RMU Tie B · Manpada ring bypass', fromName:readableNodeName(demoStart), toName:readableNodeName(demoEnd), coordinates:[[demoStart.lng,demoStart.lat],[demoStart.lng - .00034,demoStart.lat - .00018],[demoEnd.lng - .0003,demoEnd.lat - .0002],[demoEnd.lng,demoEnd.lat]], wire_size:'185 mm²', rating_kw:390, flow_kw:0, status:'normally open', voltage_kv:11 }
]
topologyEdges.push(...demoTieEdges)

function MapView({ topology, fault, selectedSolution, onHover }) {
  const mapNode = useRef(null); const mapRef = useRef(null); const [ready, setReady] = useState(false); const [error, setError] = useState('')
  const topologyRef = useRef(topology); topologyRef.current = topology
  useEffect(() => {
    if (!mapNode.current || mapRef.current) return undefined
    try {
      if (typeof maplibregl.supported === 'function' && !maplibregl.supported({ failIfMajorPerformanceCaveat: false })) { setError('WebGL unavailable — topology fallback active'); return undefined }
      const map = new maplibregl.Map({ container: mapNode.current, center: [72.9468, 19.1815], zoom: 15.15, pitch: 55, bearing: -12, maxPitch:75, attributionControl: false, style: BASEMAP_STYLE, fadeDuration: 0 })
      map.on('style.load', () => { const buildingLayers = (map.getStyle().layers || []).filter((layer) => layer['source-layer'] === 'building' && layer.type === 'fill-extrusion'); buildingLayers.forEach((layer) => { try { map.setPaintProperty(layer.id, 'fill-extrusion-color', '#c7cdd1'); map.setPaintProperty(layer.id, 'fill-extrusion-opacity', .9) } catch {} }) })
      map.on('load', () => { try { map.setLight({ anchor:'viewport', color:'#ffffff', intensity:.25 }) } catch {} setReady(true) }); map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right'); mapRef.current = map
      return () => { map.remove(); mapRef.current = null }
    } catch (cause) { setError(`3D map unavailable: ${cause.message || cause}`); return undefined }
  }, [])
  useEffect(() => {
    const map = mapRef.current; if (!map || !ready) return
    const edges = { type: 'FeatureCollection', features: topology.edges.map((edge) => ({ type: 'Feature', properties: { id: edge.id, fault: fault?.edgeIds?.includes(edge.id) || fault?.edgeId === edge.id, selected: selectedSolution?.edgeIds?.includes(edge.id) }, geometry: { type: 'LineString', coordinates: edge.coordinates } })) }
    const points = { type: 'FeatureCollection', features: topology.nodes.map((node) => {
      const faultActive = Boolean(fault?.nodeIds?.includes(node.id))
      return { type: 'Feature', properties: { ...node, telemetry: JSON.stringify(node.telemetry), fault: faultActive, faultActive, active: faultActive }, geometry: { type: 'Point', coordinates: [node.lng,node.lat] } }
    }) }
    const source = (id, data) => map.getSource(id) ? map.getSource(id).setData(data) : map.addSource(id, { type:'geojson', data })
    source('prototype-edges', edges); source('prototype-nodes', points)
    const solutionColor = selectedSolution?.id === 'SIM-01' ? '#2563eb' : selectedSolution?.id === 'SIM-02' ? '#16a34a' : '#7c3aed'
    if (!map.getLayer('prototype-edge-glow')) map.addLayer({ id:'prototype-edge-glow', type:'line', source:'prototype-edges', paint:{ 'line-color':'#000000', 'line-width':9, 'line-opacity':.14, 'line-blur':5 } })
    else { map.setPaintProperty('prototype-edge-glow','line-color','#000000'); map.setPaintProperty('prototype-edge-glow','line-width',9); map.setPaintProperty('prototype-edge-glow','line-opacity',.14) }
    if (!map.getLayer('prototype-edge')) map.addLayer({ id:'prototype-edge', type:'line', source:'prototype-edges', paint:{ 'line-color':'#000000', 'line-width':3.5, 'line-opacity':.94 } })
    else { map.setPaintProperty('prototype-edge','line-color','#000000'); map.setPaintProperty('prototype-edge','line-width',3.5) }
    if (!map.getLayer('prototype-fault-edge')) map.addLayer({ id:'prototype-fault-edge', type:'line', source:'prototype-edges', filter:['==',['get','fault'],true], paint:{ 'line-color':'#e53935', 'line-width':7, 'line-opacity':1 } })
    if (!map.getLayer('prototype-selected-edge')) map.addLayer({ id:'prototype-selected-edge', type:'line', source:'prototype-edges', filter:['==',['get','selected'],true], paint:{ 'line-color':solutionColor, 'line-width':7, 'line-opacity':1 } })
    else map.setPaintProperty('prototype-selected-edge','line-color',solutionColor)
    const nodeHaloPaint = { 'circle-color':'#000000', 'circle-radius':['case',['==',['get','type'],'pole'],5,['get','active'],29,21], 'circle-opacity':['case',['==',['get','type'],'pole'],.08,.16], 'circle-blur':.65 }
    if (!map.getLayer('prototype-node-halo')) map.addLayer({ id:'prototype-node-halo', type:'circle', source:'prototype-nodes', paint:nodeHaloPaint })
    else { map.setPaintProperty('prototype-node-halo','circle-radius',nodeHaloPaint['circle-radius']); map.setPaintProperty('prototype-node-halo','circle-opacity',nodeHaloPaint['circle-opacity']) }
    const nodePaint = { 'circle-color':['case',['get','faultActive'],'#e53935','#ffffff'], 'circle-stroke-color':['case',['get','faultActive'],'#b91c1c','#000000'], 'circle-stroke-width':['case',['==',['get','type'],'pole'],1,['get','active'],5,3], 'circle-radius':['case',['==',['get','type'],'pole'],2.5,['==',['get','type'],'substation'],14,10] }
    if (!map.getLayer('prototype-node')) map.addLayer({ id:'prototype-node', type:'circle', source:'prototype-nodes', paint:nodePaint })
    else { map.setPaintProperty('prototype-node','circle-radius',nodePaint['circle-radius']); map.setPaintProperty('prototype-node','circle-color',nodePaint['circle-color']); map.setPaintProperty('prototype-node','circle-stroke-color',nodePaint['circle-stroke-color']); map.setPaintProperty('prototype-node','circle-stroke-width',nodePaint['circle-stroke-width']) }
    if (map.getLayer('prototype-fault-node')) map.removeLayer('prototype-fault-node')
    if (!map.getLayer('prototype-label')) map.addLayer({ id:'prototype-label', type:'symbol', source:'prototype-nodes', layout:{ 'text-field':['case',['==',['get','type'],'pole'],'',['get','name']], 'text-size':14, 'text-offset':[0,1.8], 'text-anchor':'top', 'text-allow-overlap':false }, paint:{ 'text-color':'#111111','text-halo-color':'#ffffff','text-halo-width':3 } })
    const enterEdge = (event) => { map.getCanvas().style.cursor='pointer'; const item=topologyRef.current.edges.find((edge)=>edge.id===event.features?.[0]?.properties?.id); if(item) onHover({ kind:'connection', item }) }
    const enterNode = (event) => { map.getCanvas().style.cursor='pointer'; const item=topologyRef.current.nodes.find((node)=>node.id===event.features?.[0]?.properties?.id); if(item) onHover({ kind:'node', item }) }
    const leave = () => { map.getCanvas().style.cursor='' }
    map.on('mouseenter','prototype-edge',enterEdge); map.on('mouseleave','prototype-edge',leave); map.on('mouseenter','prototype-node',enterNode); map.on('mouseleave','prototype-node',leave)
    return () => { map.off('mouseenter','prototype-edge',enterEdge); map.off('mouseleave','prototype-edge',leave); map.off('mouseenter','prototype-node',enterNode); map.off('mouseleave','prototype-node',leave) }
  }, [topology.edges.length, topology.nodes.length, fault, selectedSolution, ready, onHover])
  const projection = (lng, lat) => [((lng - 72.91) / .11) * 900 + 50, (1 - (lat - 19.15) / .07) * 600 + 50]
  return <div className={`map-shell ${error ? 'map-fallback-mode' : ''}`}><div ref={mapNode} className="map-canvas" />{error && <div className="map-error"><strong>{error}</strong><span>The local topology simulation is still available below.</span></div>}{error && <svg className="topology-fallback" viewBox="0 0 1000 700">{topology.edges.map((edge) => <polyline key={edge.id} className={fault?.edgeIds?.includes(edge.id) || fault?.edgeId === edge.id ? 'fallback-edge affected' : 'fallback-edge'} points={edge.coordinates.map(([lng,lat])=>projection(lng,lat).join(',')).join(' ')} />)}{topology.nodes.map((node) => { const [x,y]=projection(node.lng,node.lat); return <g key={node.id}><circle className={fault?.nodeIds?.includes(node.id) ? 'fallback-node affected' : 'fallback-node'} cx={x} cy={y} r={fault?.nodeIds?.includes(node.id) ? 14 : 10} /><text x={x+12} y={y-10}>{readableNodeName(node)}</text></g>})}</svg>}<div className="map-status"><span className="live-dot" /> LOCAL TOPOLOGY <span className="divider" /> {topology.nodes.length} NODES <span className="divider" /> FRONTEND SIMULATION</div><div className="map-legend"><span><i className="cyan" /> energized</span><span><i className="amber" /> watch</span><span><i className="red" /> affected</span></div></div>
}

const telemetryLabel = (key) => ({ current_load_kw:'CURRENT LOAD', loading_percentage:'LOADING', voltage_pu:'VOLTAGE', status:'STATUS' }[key] || key.replaceAll('_',' ').toUpperCase())
const telemetryValue = (key, value) => key === 'loading_percentage' ? `${value}%` : key === 'voltage_pu' ? `${value} pu` : key === 'current_load_kw' ? `${value} kW` : value

function App() {
  const [fault, setFault] = useState(null); const [selectedSolution, setSelectedSolution] = useState(null); const [hover, setHover] = useState(null)
  const [topology, setTopology] = useState(() => ({ nodes: topologyNodes, edges: topologyEdges }))
  useEffect(() => {
    const timer = window.setInterval(() => {
      setTopology((current) => {
        const activeEdges = new Set(fault?.edgeIds || [])
        const activeNodes = new Set(fault?.nodeIds || [])
        return {
          nodes: current.nodes.map((node, index) => {
            const baseLoad = Number(node.telemetry.current_load_kw) || 0
            const faulted = activeNodes.has(node.id)
            return { ...node, telemetry: { ...node.telemetry, current_load_kw: faulted ? Math.round((node.capacity_kw || 200) * .98) : Math.max(0, Math.round(baseLoad + Math.sin(Date.now() / 7000 + index) * 2)), loading_percentage: faulted ? 98 : Math.min(100, Math.max(0, Math.round(Number(node.telemetry.loading_percentage || 0) + Math.sin(Date.now() / 9000 + index)))), voltage_pu: faulted ? '0.91' : node.telemetry.voltage_pu, status: faulted ? 'fault' : node.telemetry.status } }
          }),
          edges: current.edges.map((edge, index) => {
            const faulted = activeEdges.has(edge.id)
            const baseFlow = Number(edge.flow_kw) || 0
            return { ...edge, flow_kw: faulted ? (edge.id === fault?.edgeId ? edge.rating_kw + 17 : Math.round(edge.rating_kw * .9)) : Math.max(0, Math.round(baseFlow + Math.sin(Date.now() / 7000 + index) * 2)), status: faulted ? 'fault' : edge.status }
          })
        }
      })
    }, 5000)
    return () => window.clearInterval(timer)
  }, [fault])
  const solutions = useMemo(() => { if (!fault) return []; const edge = topology.edges.find((item) => item.id === fault.edgeId); const tieA = topology.edges.find((item) => item.id === 'RMU-TIE-A'); const tieB = topology.edges.find((item) => item.id === 'RMU-TIE-B'); return [
    { id:'SIM-01', rank:1, title:'Close RMU Tie A and transfer the load', description:`Open the faulted ${edge.displayName}, then close Ring Main Unit Tie A. The same source feeds the same destination through the Market Loop bypass; predicted loading falls below the ${tieA.rating_kw} kW tie rating.`, edgeIds:[tieA.id], confidence:94, eta:'4 min' },
    { id:'SIM-02', rank:2, title:'Use the alternate RMU ring path', description:`Keep the same source and destination but use Ring Main Unit Tie B through the Manpada ring. This is a separate physical route with ${tieB.rating_kw} kW of capacity, so the red path is isolated rather than repaired.`, edgeIds:[tieB.id], confidence:88, eta:'6 min' },
    { id:'SIM-03', rank:3, title:'Reconduct the faulted path', description:`Replace the ${edge.wire_size} conductor on the affected ${edge.displayName} path with a higher-rated wire. This directly fixes the overloaded red route; no RMU switching is required.`, edgeIds:fault.edgeIds, confidence:79, eta:'2 days' },
  ] }, [fault, topology])
  const simulateFault = () => { const edge = topology.edges[6]; const downstream = topology.edges.find((item) => item.from === edge.to); const edgeIds = [edge.id, downstream?.id].filter(Boolean); const nodeIds = [edge.from,edge.to,downstream?.to].filter(Boolean); setFault({ id:`fault_${edge.id}`, edgeId:edge.id, edgeIds, nodeIds, title:'Feeder overload detected', reason:`${edge.displayName} is carrying ${Math.round(edge.rating_kw + 17)} kW against a ${edge.rating_kw} kW rating. The affected path is ${edge.fromName} → ${edge.toName}${downstream ? ` → ${downstream.toName}` : ''}.`, created:'just now' }); setTopology((current) => ({ nodes: current.nodes.map((node) => nodeIds.includes(node.id) ? { ...node, telemetry: { ...node.telemetry, current_load_kw: Math.round((node.capacity_kw || 200) * .98), loading_percentage: 98, voltage_pu:'0.91', status:'fault' } } : node), edges: current.edges.map((item) => edgeIds.includes(item.id) ? { ...item, flow_kw: item.id === edge.id ? item.rating_kw + 17 : Math.round(item.rating_kw * .9), status:'fault' } : item) })); setSelectedSolution(null) }
  const clearFault = () => { setFault(null); setTopology({ nodes: topologyNodes, edges: topologyEdges }); setSelectedSolution(null) }
  const hoveredText = hover?.kind === 'connection' ? topology.edges.find((item) => item.id === hover.item.id) : null
  const hoveredNode = hover?.kind === 'node' ? topology.nodes.find((item) => item.id === hover.item.id) : null
  return <main className="dashboard"><aside className="sidebar"><div className="brand-mark small">GB<span>◈</span></div><nav><button className="nav-active">◉<span>Overview</span></button><button>⌁<span>Topology</span></button><button>⌁<span>Telemetry</span></button><button>◇<span>Incidents</span></button><button>⊙<span>Settings</span></button></nav><div className="sidebar-bottom"><div className="user-avatar">DEMO</div><span>LOCAL / 01</span></div></aside><section className="workspace"><header className="topbar"><div><p className="eyebrow">DISTRIBUTION CONTROL / PROTOTYPE</p><h2>Network overview <span className="live-pill"><i /> SIMULATION</span></h2></div><div className="top-actions"><span className="connection online"><i /> LOCAL DATA</span><button className="icon-button">⌕</button><button className="icon-button">⋮</button></div></header><div className="content-grid"><section className="map-panel"><MapView topology={topology} fault={fault} selectedSolution={selectedSolution} onHover={setHover} />{hoveredText && <div className="node-inspector connection-inspector"><button onClick={() => setHover(null)}>×</button><span>CONNECTION / LIVE TELEMETRY</span><strong>{hoveredText.displayName || hoveredText.id}</strong><small>ROUTE <b>{hoveredText.fromName} → {hoveredText.toName}</b></small><small>WIRE SIZE <b>{hoveredText.wire_size}</b></small><small>RATING <b>{hoveredText.rating_kw} kW</b></small><small>FLOW <b>{hoveredText.flow_kw} kW</b></small><small>VOLTAGE <b>{hoveredText.voltage_kv} kV</b></small><small>STATUS <b>{fault?.edgeIds?.includes(hoveredText.id) ? 'AFFECTED' : hoveredText.status}</b></small></div>}{hoveredNode && <div className="node-inspector"><button onClick={() => setHover(null)}>×</button><span>NODE / LIVE TELEMETRY</span><strong>{hoveredNode.name}</strong><small>ID <b>{hoveredNode.id}</b></small>{Object.entries(hoveredNode.telemetry).map(([key,value])=><small key={key}>{telemetryLabel(key)} <b>{telemetryValue(key, value)}</b></small>)}</div>}</section><aside className="right-rail"><div className="rail-heading"><span>DECISION CENTER</span><span className="priority">PROTOTYPE</span></div><button className="simulate-button" onClick={fault ? clearFault : simulateFault}>{fault ? 'CLEAR SIMULATED FAULT' : 'SIMULATE FAULT'}</button>{fault ? <div className="anomaly-card selected"><div className="anomaly-top"><span className="alert-icon">!</span><span>ANOMALY DETECTED</span><time>{fault.created}</time></div><h3>{fault.title}</h3><p>{fault.reason}</p><div className="affected"><span>AFFECTED ASSETS</span><strong>{fault.nodeIds.length} nodes <em>·</em> {fault.edgeIds.length} connections</strong></div></div> : <div className="empty-anomaly"><span className="pulse-ring" /> No active faults<br /><small>Use the simulation button to test detection.</small></div>}<div className="solutions-heading"><span>MILP-STYLE SUGGESTIONS</span><small>{solutions.length} OPTIONS</small></div>{solutions.length ? <div className="solution-list">{solutions.map((solution)=><button key={solution.id} className={`solution ${selectedSolution?.id===solution.id?'selected':''}`} onClick={()=>setSelectedSolution(selectedSolution?.id===solution.id?null:solution)}><span className="rank">0{solution.rank}</span><span className="solution-copy"><strong>{solution.title}</strong><small>{solution.description}</small><span className="solution-meta"><b>{solution.confidence}%</b> confidence <em>·</em> {solution.eta}</span></span><span className="chevron">›</span></button>)}</div> : <p className="muted prototype-note">Suggestions appear after a simulated fault.</p>}<div className="activity-heading"><span>SIMULATION ACTIVITY</span><small>LOCAL</small></div><div className="activity-list"><div className="activity"><i className={fault?'red':'cyan'} /><div><strong>{fault?'Fault simulated on '+(topology.edges.find((item)=>item.id===fault.edgeId)?.displayName || fault.edgeId):'Telemetry stream is nominal'}</strong><small>Frontend-only state</small></div></div>{selectedSolution && <div className="activity"><i className="cyan" /><div><strong>{selectedSolution.id} suggestion selected</strong><small>Advisory only — no execution available</small></div></div>}</div></aside></div></section></main>
}

export default App
