# Shared paper figure theme
# Loaded by the figure renderers beside this file

suppressPackageStartupMessages({
  library(ggplot2)
})

paper_palette <- c(
  ink = "#1B1B1B",
  muted = "#666666",
  grid = "#D9DEE7",
  blue = "#0072B2",
  sky = "#56B4E9",
  teal = "#009E73",
  gold = "#E69F00",
  vermillion = "#D55E00",
  purple = "#CC79A7"
)

paper_discrete_values <- unname(paper_palette[c(
  "blue", "vermillion", "teal", "purple", "gold", "sky"
)])
paper_rainbow <- c("#FFFFFF", "#313695", "#00A6CA", "#00CCBC", "#90EB9D", "#FFFF8C", "#F9D057", "#F29E2E", "#E76818", "#D7191C")

theme_paper <- function(base_size = 10, base_family = "") {
  ggplot2::theme_minimal(base_size = base_size, base_family = base_family) +
    ggplot2::theme(
      plot.title = ggplot2::element_blank(),
      plot.subtitle = ggplot2::element_blank(),
      plot.caption = ggplot2::element_text(
        size = ggplot2::rel(0.82),
        color = paper_palette[["muted"]],
        hjust = 0,
        margin = ggplot2::margin(t = 6)
      ),
      axis.title = ggplot2::element_text(color = paper_palette[["ink"]]),
      axis.text = ggplot2::element_text(color = paper_palette[["muted"]]),
      panel.grid.minor = ggplot2::element_blank(),
      panel.grid.major = ggplot2::element_line(
        color = paper_palette[["grid"]],
        linewidth = 0.25
      ),
      legend.title = ggplot2::element_blank(),
      legend.position = "bottom",
      legend.box = "horizontal",
      legend.key.width = grid::unit(1.1, "lines"),
      strip.background = ggplot2::element_rect(fill = "#F4F6F8", color = NA),
      strip.text = ggplot2::element_text(
        face = "bold",
        color = paper_palette[["ink"]]
      ),
      plot.margin = ggplot2::margin(6, 8, 6, 8)
    )
}

scale_colour_paper <- function(..., values = paper_discrete_values) {
  ggplot2::scale_colour_manual(..., values = values)
}
