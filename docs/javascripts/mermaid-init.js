document$.subscribe(function () {
  if (typeof mermaid !== "undefined") {
    mermaid.initialize({
      startOnLoad: false,
      theme: "base",
      themeVariables: {
        background: "#0f1830",
        primaryColor: "#131f3d",
        primaryTextColor: "#e6edf7",
        primaryBorderColor: "#2a3860",
        lineColor: "#7a8bad",
        secondaryColor: "#1a2749",
        tertiaryColor: "#0b1220",
        mainBkg: "#131f3d",
        secondBkg: "#1a2749",
        clusterBkg: "#0f1830",
        clusterBorder: "#2a3860",
        edgeLabelBackground: "#0f1830",
        fontFamily: "Inter, -apple-system, sans-serif",
        fontSize: "13px",
        nodeBorder: "#2a3860",
        titleColor: "#e6edf7",
        textColor: "#c8d3e6"
      },
      flowchart: {
        curve: "basis",
        padding: 20,
        nodeSpacing: 50,
        rankSpacing: 60
      }
    });
    mermaid.run({ nodes: document.querySelectorAll(".mermaid") });
  }
});
