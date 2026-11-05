# Plot the saved grid activity for each cumulative location tier
args <- commandArgs(trailingOnly = TRUE)
script <- sub("^--file=", "", commandArgs()[grepl("^--file=", commandArgs())])
source(file.path(dirname(script), "paper_theme.R"))
source(file.path(dirname(script), "figure_save.R"))
if (length(args) == 3 && args[3] == "allocation_examples") {
  cases <- jsonlite::fromJSON(args[1], simplifyVector = FALSE)
  plots <- list()
  index <- function(ids, size, origin_x, origin_y) {
    parts <- regmatches(ids, regexec("r([0-9]+)c([0-9]+)", ids))
    rows <- vapply(parts, function(p) as.numeric(p[2]), numeric(1))
    cols <- vapply(parts, function(p) as.numeric(p[3]), numeric(1))
    data.frame(x = origin_x + cols * size, y = origin_y - (rows + 1) * size)
  }
  for (i in seq_along(cases)) {
    case <- cases[[i]]; grid <- case$grid
    cells <- index(vapply(case$cells, `[[`, "", "cell"), 100, grid$origin_x, grid$origin_y)
    cells$built <- vapply(case$cells, `[[`, 0, "built")
    parents <- index(unique(vapply(case$cells, `[[`, "", "parent")), 1000, grid$origin_x, grid$origin_y)
    parents$id <- unique(vapply(case$cells, `[[`, "", "parent"))
    parents$weight <- vapply(parents$id, function(id) if (is.null(case$weights[[id]])) 0 else case$weights[[id]], 0)
    colours <- col2rgb(scales::col_numeric(paper_rainbow, c(0, max(parents$weight)))(parents$weight)) / 255
    parents$label_colour <- ifelse(colSums(colours * c(.2126, .7152, .0722)) < .45, "white", "black")
    xmin <- min(parents$x); xmax <- max(parents$x) + 1000
    ymin <- min(parents$y); ymax <- max(parents$y) + 1000
    cx <- (xmin + xmax) / 2; cy <- (ymin + ymax) / 2
    span <- max(xmax - xmin, ymax - ymin) * 1.35
    limits_x <- cx + c(-.5, .5) * span; limits_y <- cy + c(-.5, .5) * span
    background <- as.raster(matrix(unlist(case$basemap$colours), nrow = length(case$basemap$colours), byrow = TRUE))
    base <- ggplot() + annotation_raster(background, limits_x[1], limits_x[2], limits_y[1], limits_y[2]) +
      coord_equal(xlim = limits_x, ylim = limits_y, expand = FALSE) +
      theme_void(base_size = 9) + theme(plot.title = element_text(size = 9.5, face = "bold"),
      panel.border = element_rect(fill = NA, colour = "grey50", linewidth = .3),
      legend.position = "bottom", legend.title = element_text(size = 9), legend.text = element_text(size = 8.5),
      plot.margin = margin(4, 5, 12, 5))
    outline <- geom_rect(data = parents, aes(xmin = x, xmax = x + 1000, ymin = y, ymax = y + 1000),
                         fill = NA, colour = "grey50", linewidth = .25)
    area <- base + geom_rect(data = cells, aes(xmin = x, xmax = x + 100, ymin = y, ymax = y + 100),
                             fill = "#0072B2", alpha = .22, colour = NA) + outline
    if (!is.null(case$geometry)) {
      boundary <- do.call(rbind, lapply(case$geometry, as.data.frame))
      area <- area + geom_path(data = boundary, aes(x = x, y = y, group = ring), colour = "#0072B2", linewidth = .65)
    } else {
      area <- area + geom_rect(data = parents, aes(xmin = x, xmax = x + 1000, ymin = y, ymax = y + 1000),
                               fill = "#0072B2", alpha = .22, colour = "#0072B2", linewidth = .55)
      seed <- parents[parents$id == case$seed, ]
      area <- area + geom_point(data = seed, aes(x = x + 500, y = y + 500), shape = 4, size = 2)
    }
    area <- area + annotate("segment", x = limits_x[1] + .08 * span, xend = limits_x[1] + .08 * span + 1000,
                            y = limits_y[1] + .08 * span, yend = limits_y[1] + .08 * span, linewidth = .7) +
      annotate("text", x = limits_x[1] + .08 * span + 500, y = limits_y[1] + .13 * span, label = "1 km", size = 3)
    legend <- guides(fill = guide_colourbar(title.position = "top", barwidth = grid::unit(1.5, "in"), barheight = grid::unit(.09, "in")))
    built <- base + geom_rect(data = cells, aes(xmin = x, xmax = x + 100, ymin = y, ymax = y + 100, fill = built), alpha = .8) +
      outline + scale_fill_gradientn("Built surface (m² / 100 m cell)", colours = paper_rainbow, limits = c(0, max(cells$built)),
                                     labels = function(x) format(x, scientific = FALSE, big.mark = ",")) + legend
    weighted <- base + geom_rect(data = parents, aes(xmin = x, xmax = x + 1000, ymin = y, ymax = y + 1000, fill = weight),
                                 colour = "grey50", linewidth = .3, alpha = .8) +
      scale_fill_gradientn("Allocated share (%)", colours = paper_rainbow, limits = c(0, max(parents$weight)), labels = function(x) format(100 * x, trim = TRUE)) +
      geom_text(data = parents[parents$weight > 0, ], aes(x = x + 500, y = y + 500,
                label = sprintf("%4.1f", 100 * weight), colour = I(label_colour)),
                family = "mono", hjust = .5, vjust = .5, size = 2.7) + legend
    titles <- c(if (case$summary$method == "actual_street_geometry_100m_support") "Mapped street geometry" else if (is.null(case$geometry)) "Inferred 1 km cells" else "Mapped postcode boundary",
                paste("Built surface", case$summary$ghsl_epoch), "Weighted 1 km cells")
    row <- list(area, built, weighted)
    for (k in seq_along(row)) plots[[length(plots) + 1]] <- row[[k]] + labs(title = paste(LETTERS[(i - 1) * 3 + k], titles[k]))
  }
  png(paste0(args[2], ".png"), width = 8.2, height = 2.8 * length(cases), units = "in", res = 320, type = "cairo", bg = "white")
  grid::grid.newpage()
  grid::pushViewport(grid::viewport(layout = grid::grid.layout(length(cases), 3)))
  grobs <- lapply(plots, ggplotGrob)
  for (row in seq_along(cases)) {
    indices <- (row - 1) * 3 + seq_len(3)
    heights <- do.call(grid::unit.pmax, lapply(grobs[indices], `[[`, "heights"))
    widths <- do.call(grid::unit.pmax, lapply(grobs[indices], `[[`, "widths"))
    for (k in indices) { grobs[[k]]$heights <- heights; grobs[[k]]$widths <- widths }
  }
  for (k in seq_along(grobs)) {
    grid::pushViewport(grid::viewport(layout.pos.row = ceiling(k / 3), layout.pos.col = (k - 1) %% 3 + 1))
    grid::grid.draw(grobs[[k]])
    grid::popViewport()
  }
  grid::popViewport()
  grid::grid.text("© OpenStreetMap contributors", x = .99, y = .006, just = "right", gp = grid::gpar(fontsize = 6, col = "grey35"))
  dev.off()
  for (row in seq_along(cases)) {
    png(paste0(args[2], "-", row, ".png"), width = 8.2, height = 2.8, units = "in", res = 320, type = "cairo", bg = "white")
    grid::grid.newpage()
    grid::pushViewport(grid::viewport(layout = grid::grid.layout(1, 3)))
    for (column in seq_len(3)) {
      grid::pushViewport(grid::viewport(layout.pos.row = 1, layout.pos.col = column))
      grid::grid.draw(grobs[[(row - 1) * 3 + column]])
      grid::popViewport()
    }
    grid::popViewport()
    grid::grid.text("© OpenStreetMap contributors", x = .99, y = .012, just = "right", gp = grid::gpar(fontsize = 6, col = "grey35"))
    dev.off()
  }
  quit(save = "no")
}
data <- read.csv(args[1])
if (length(args) == 3 && args[3] == "country_grid") {
  data <- data[data$country != "Unassigned", ]
  countries <- sort(unique(data$country))
  colours <- setNames(hcl.colors(length(countries), "Dark 3"), countries)
  colours[names(colours) %in% c("Border", "Unassigned")] <- "grey50"
  maximum <- max(data$applicants, data$inventors)
  p <- ggplot(data, aes(applicants, inventors, colour = country)) +
    geom_abline(slope = 1, intercept = 0, colour = "grey60", linewidth = .3, linetype = 2) +
    geom_point(size = .55, alpha = .5) +
    scale_x_continuous(trans = "log1p", limits = c(0, maximum), breaks = c(0, 1, 10, 100, 1000, 10000, 100000),
                       labels = function(x) format(x, scientific = FALSE, big.mark = ",")) +
    scale_y_continuous(trans = "log1p", limits = c(0, maximum), breaks = c(0, 1, 10, 100, 1000, 10000, 100000),
                       labels = function(x) format(x, scientific = FALSE, big.mark = ",")) +
    coord_equal() + scale_colour_manual(values = colours) +
    guides(colour = guide_legend(ncol = 7, override.aes = list(size = 2, alpha = 1))) +
    labs(x = "Applicant fractional DOCDB families per 1 km cell",
         y = "Inventor fractional DOCDB families per 1 km cell") +
    theme(legend.key.width = grid::unit(.6, "lines"))
  save_paper_figure(p, args[2], width = 7.5, height = 7)
} else if (length(args) == 4 && args[3] == "postcode_sizes") {
  names(data)[names(data) == "area_lower_km2"] <- "lower"
  names(data)[names(data) == "area_upper_km2"] <- "upper"
  streets <- read.csv(args[4])
  streets <- data.frame(construction_method = "street_length",
                        lower = streets$length_lower_km,
                        upper = streets$length_upper_km,
                        allocations = streets$allocations)
  data <- rbind(streets, data)
  data$construction_method[data$construction_method == "postcode_point_single_cell"] <- "postcode_1km_centre_flood_fill"
  data <- aggregate(allocations ~ construction_method + lower + upper, data, sum)
  methods <- c(street_length = "Street length (km)",
               qualified_osm_postcode_polygon = "Mapped postcode area (km²)",
               postcode_1km_centre_flood_fill = "Inferred postcode area (km²)")
  data <- data[data$construction_method %in% names(methods), ]
  data$construction_method <- factor(data$construction_method, levels = names(methods))
  panels <- data.frame(construction_method = factor(names(methods), levels = names(methods)),
                       panel = c("A", "B", "C"))
  p <- ggplot(data, aes(x = (lower + upper) / 2, y = allocations)) +
    geom_col(width = 1, fill = paper_palette[["blue"]]) +
    geom_text(data = panels, aes(x = -Inf, y = Inf, label = panel), inherit.aes = FALSE,
              hjust = 0, vjust = -.8, fontface = "bold", size = 4) +
    scale_x_continuous(limits = c(0, NA)) +
    scale_y_continuous(labels = function(x) format(x, big.mark = ",", scientific = FALSE, trim = TRUE)) +
    coord_cartesian(clip = "off") +
    facet_wrap(~ construction_method, scales = "free", strip.position = "bottom",
               labeller = as_labeller(methods)) +
    labs(x = NULL, y = "Address observations") +
    theme(strip.placement = "outside", strip.background = element_blank(),
          strip.text.x = element_text(face = "plain"),
          panel.spacing.x = grid::unit(1.4, "lines"),
          plot.margin = margin(t = 22, r = 8, b = 6, l = 6))
  save_paper_figure(p, args[2], width = 10, height = 3.6)
} else if (length(args) == 5 && args[3] == "tl3_grid") {
  if ("place" %in% names(data)) {
    data$x <- data$x / (data$view_width_km / 2)
    data$y <- data$y / (data$view_width_km / 2)
    data$role <- data$place
    data$role <- factor(data$role, levels = unique(data$place))
  } else data$role <- factor(data$role, levels = c("applicants", "inventors"), labels = c("Applicants", "Inventors"))
  data$resolution <- factor(data$resolution, levels = c("TL3", "1 km"))
  regional <- data[data$resolution == "TL3", c("region_id", "role", "resolution", "density")]
  vertices <- read.csv(args[4])
  if ("place" %in% names(vertices)) {
    vertices$x <- vertices$x / (vertices$view_width_km / 2)
    vertices$y <- vertices$y / (vertices$view_width_km / 2)
    vertices$role <- factor(vertices$place, levels = levels(data$role))
    polygons <- merge(vertices, regional, by = c("region_id", "role"))
  } else polygons <- merge(vertices, regional, by = "region_id")
  outlines <- rbind(polygons, transform(polygons, resolution = "1 km"))
  labels <- expand.grid(resolution = levels(data$resolution), role = levels(data$role))
  labels$panel <- LETTERS[seq_len(nrow(labels))]
  half <- as.numeric(args[5])
  p <- ggplot() +
    geom_polygon(data = polygons, aes(x, y, group = group, subgroup = ring, fill = density), rule = "evenodd") +
    geom_tile(data = data[data$resolution == "1 km", ], aes(x, y, fill = density, width = if ("place" %in% names(data)) 2 / view_width_km else 1,
              height = if ("place" %in% names(data)) 2 / view_width_km else 1)) +
    geom_path(data = outlines, aes(x, y, group = ring), colour = "grey50", linewidth = .2) +
    geom_label(data = labels, aes(x = -half + half * .05, y = half - half * .05, label = panel), hjust = 0, vjust = 1,
               fill = "white", linewidth = 0, fontface = "bold", size = 3) +
    facet_grid(role ~ resolution) + coord_equal(xlim = c(-half, half), ylim = c(-half, half), expand = FALSE) +
    scale_fill_gradientn(colours = paper_rainbow,
                         limits = c(0, max(data$density)),
                         trans = "log1p", name = "Fractional DOCDB families per km²",
                         breaks = c(0.1, 1, 10, 100, 500),
                         labels = function(x) format(x, scientific = FALSE, trim = TRUE, drop0trailing = TRUE),
                         guide = guide_colourbar(barwidth = grid::unit(10, "cm"), title.position = "top")) +
    scale_x_continuous(breaks = c(-10, 0, 10)) +
    theme(panel.spacing = grid::unit(.5, "cm"), legend.title = element_text()) +
    labs(x = "Easting from centre (km)", y = "Northing from centre (km)")
  if ("place" %in% names(data)) {
    bars <- unique(data[c("role", "resolution", "view_width_km")])
    bars$length_km <- vapply(bars$view_width_km, function(w) max(c(1, 2, 5, 10, 20, 50, 100)[c(1, 2, 5, 10, 20, 50, 100) <= w / 4]), numeric(1))
    p <- p + scale_x_continuous(breaks = NULL) + scale_y_continuous(breaks = NULL) +
      labs(x = NULL, y = NULL) +
      geom_segment(data = bars, aes(x = -.9, xend = -.9 + 2 * length_km / view_width_km, y = -.9, yend = -.9), linewidth = .8) +
      geom_label(data = bars, aes(x = -.9, y = -.8, label = paste(length_km, "km")), hjust = 0, size = 2.5, linewidth = 0, fill = "white")
  }
  save_paper_figure(p, args[2], width = 7.5, height = 7)
} else {
data$tier <- factor(data$tier, levels = c("direct", "direct_street", "direct_street_pcdb", "direct_street_pcdb_rg"),
                    labels = c("Direct", "Direct + street", "Direct + street + mapped postcode",
                               "Including point-inferred postcode"))
p <- ggplot(data, aes(x = x, y = y, fill = log1p(activity))) +
  geom_tile(width = 1000, height = 1000) + coord_equal() + facet_wrap(~ tier) +
  scale_x_continuous(breaks = function(x) pretty(x, n = 2)) +
  scale_y_continuous(breaks = function(x) pretty(x, n = 2)) +
  scale_fill_viridis_c(name = "log(1 + fractional families)") + labs(x = "Easting (m)", y = "Northing (m)")
save_paper_figure(p, args[2], width = 9, height = 3)

}
