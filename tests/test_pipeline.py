import unittest

class PipelineImports(unittest.TestCase):

    def test_entry_points_import(self):
        import geocoding.geocode
        import geographic_uncertainty.prepare_geographic_inputs
        import geographic_uncertainty.contracts
        import geographic_uncertainty.retrieve_and_rasterise
        import weighting.register_ghsl
        import weighting.sample_built_surface
        import weighting.allocate_to_grid
        import aggregation.aggregate_grid_activity
if __name__ == '__main__':
    unittest.main()

class OfflineControls(unittest.TestCase):

    def test_epoch_does_not_use_future_or_available_substitute(self):
        from weighting.allocate_to_grid import select_available_epoch
        self.assertEqual(select_available_epoch(2002, [1995, 2000, 2005], [1995, 2005]), (2000, False))
        self.assertEqual(select_available_epoch(1990, [1995, 2000], [1995]), (None, False))

    def test_invalid_built_surface_weight_rejected(self):
        from weighting.allocate_to_grid import _decimal
        for value in ['-1', 'NaN', 'Infinity', 'invented']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _decimal(value)

class ProductionConfiguration(unittest.TestCase):
    def test_country_scope_is_shared_and_has_35_countries(self):
        from run import COUNTRIES
        from geographic_uncertainty import routing, prepare_geographic_inputs
        from figures_tables import paper_outputs
        self.assertEqual(len(COUNTRIES), 35)
        self.assertIs(routing.COUNTRIES, COUNTRIES)
        self.assertIs(prepare_geographic_inputs.COUNTRIES, COUNTRIES)
        self.assertIs(paper_outputs.COUNTRIES, COUNTRIES)
        self.assertTrue({'AU', 'CA', 'CL', 'CO', 'CR', 'MX', 'NZ', 'TR'} <= set(COUNTRIES))

    def test_direct_geocoder_uses_production_defaults(self):
        from unittest.mock import patch
        from geocoding import geocode
        argv = ['patent-code geocode', 'run', '--input', '/data/addresses.csv',
                '--boundaries', '/data/boundaries.json', '--region-preflight', '/data/regions.json',
                '--output', '/data/output', '--service-identity', '/data/service.json', '--endpoint', 'http://localhost:8184', '--request-budget', '1000']
        with patch('sys.argv', argv), patch.object(geocode, 'run', return_value={'counts': {}}) as called, patch('builtins.print'):
            geocode.main()
        args = called.call_args.kwargs
        for name, value in dict(regional_only=True, normalized_agreement=True, workers=8,
                                max_in_flight=8, timeout_seconds=31, max_attempts=2, delay_seconds=0).items():
            self.assertEqual(args[name], value)

    def test_old_unbounded_selection_cannot_be_enabled(self):
        from pathlib import Path
        from geocoding.search_and_select import run
        with self.assertRaisesRegex(ValueError, 'Only the source-region bounded'):
            run(input_path=Path('/not-read'), boundary_path=Path('/not-read'),
                output_dir=Path('/not-created'), cache_dir=Path('/not-created'),
                tl3_property='code', endpoint='http://localhost:8184', user_agent='test', regional_only=False)
