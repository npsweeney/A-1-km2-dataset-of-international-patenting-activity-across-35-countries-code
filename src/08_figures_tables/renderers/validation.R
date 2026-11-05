# Compare the recorded external references using the same figure layout
args <- commandArgs(trailingOnly = TRUE)
script <- sub("^--file=", "", commandArgs()[grepl("^--file=", commandArgs())])
source(file.path(dirname(script), "paper_theme.R"))
source(file.path(dirname(script), "figure_save.R"))
data <- jsonlite::fromJSON(args[1])
labels <- c(morrison = "Morrison", de_rassenfosse = "de Rassenfosse")
refs <- labels[names(data$references)]
if (any(is.na(refs))) stop("Unknown reference label")
colours <- unname(paper_palette[c("blue", "vermillion", "teal")][seq_along(refs)])
rows <- function(section) do.call(rbind, lapply(names(refs), function(name) {
  x <- data$references[[name]][[section]]
  x$reference <- factor(refs[[name]], levels = unname(refs))
  x
}))
base_plot <- function(x, variable, axis) {
  x$group <- factor(x$group, levels = rev(unique(x$group)))
  x$y <- as.numeric(x$group) + (as.numeric(x$reference) - (length(refs) + 1) / 2) * .26
  ggplot(x, aes(x = .data[[variable]], y = y, colour = reference)) +
    scale_y_continuous(breaks = seq_along(levels(x$group)), labels = levels(x$group),
                       limits = c(.5, length(levels(x$group)) + .5)) +
    scale_colour_paper(values = colours) + theme_paper() +
    theme(panel.grid.major.y = element_blank()) +
    labs(x = axis, y = NULL)
}
cells <- rows("cells")
cells$group <- paste0(tools::toTitleCase(cells$actor), "s\nn = ", cells$n, " applications")
figure <- base_plot(cells, "mean_agreement_percent",
                    "Mean within-patent 1 km cell overlap (%)") +
  geom_point(size = 2.4) +
  scale_x_continuous(limits = c(0, 100), breaks = c(0, 25, 50, 75, 100),
                     expand = expansion(mult = .05)) +
  theme(legend.position = "bottom")
save_paper_figure(figure, args[2], width = 8, height = 3.2)
