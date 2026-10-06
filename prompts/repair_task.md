You are repairing gaps in a road network read from an 1890s Swedish map sheet (Häradsekonomiska kartan). Use ONLY the Read tool on the case images listed below, in order, and nothing else (no other files, no code, no searching). Then write your answer with the Write tool to <output path> (a single JSON object, nothing else in the file) and reply with one line saying how many cases you connected and left.

Each case image is a 400 m x 400 m crop of the map, 600 x 600 px, north up; image coordinates u run right and v run down from (0, 0) at the top-left corner. Lines already in the network are drawn on top: RED is the main network, BLUE is a loose piece that is not connected to it. Circles mark candidate points: A1, A2, ... on the blue piece and B1, B2, ... on the red network.

For each case decide whether the map itself shows a road connecting the blue piece to the red network here. If it does, answer "connect" with the from-point (an A label), the to-point (a B label) and the course as image waypoints [[u, v], ...] that follow the road drawn on the map between them (start at the A point, end at the B point, a waypoint wherever the road bends). If the map does not show such a road (the gap is a field, a boundary, water, or the blue piece is not a road), answer "leave". This is a best guess for a demonstration network: connect when the drawing reasonably supports it, but never invent a road that is not drawn. Give a one-line reason saying what is visible.

Answer with this JSON shape: {"cases": [{"case_id": "R001", "decision": "connect" | "leave", "from": "A1", "to": "B1", "waypoints_uv": [[u, v], ...], "reason": "..."}]}. Include every case below exactly once.

Cases:
