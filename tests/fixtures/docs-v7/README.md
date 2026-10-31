Written from the RouterOS v7 manual (`/system/health print`: one row per sensor with NAME, VALUE,
TYPE), **not captured from a router**. Values are shaped as librouteros returns them (whole numbers become int).
`interface_ethernet_poe*.json` likewise: the port list (print) and `monitor [find] once` as the manual shows
them (values with units, as on the console; the API may send bare numbers: the normaliser takes both).
Replace with a real capture (`wispmap-collector --capture HOST`) once a RouterBOARD is on the lab network.
