import unittest
from tests.test_ui_backend_adaptation import native_config,rendered_settings_page
from tests.ui_render_probe import run_collect_probe,node_available
from web.settings_editor import apply_settings_payload
@unittest.skipUnless(node_available(),'Node required for real page script collect')
class UnsavedProviderRenameTests(unittest.TestCase):
 def test_pending_selections_survive_unrelated_rename(self):
  self.check('exa_1','exa renamed','deepseek','')
 def test_pending_selection_tracks_its_renamed_provider(self):
  self.check('deepseek','deepseek renamed','deepseek','deepseek')
 def check(self,old,new,jev,ds):
  scenario={'name':'pending-rename','set':{'native_jev_provider':jev,'native_jev_deepseek_provider':ds,'native_jev_model':'unsaved-model','native_jev_env_file':'/review/unsaved.env'},'renameProvider':[{'from':old,'to':new}]}
  out=run_collect_probe(rendered_settings_page(),[scenario])['results'][0]
  self.assertNotIn('error',out)
  result=apply_settings_payload(native_config(),out['payload'],str)['tag_classifier']['grounded_judge']['native_jev']
  self.assertEqual(result['jev_provider'],new if jev==old else jev)
  self.assertEqual(result['deepseek_search_provider'],new if ds==old else ds)
  self.assertEqual(result['model'],'unsaved-model')
  self.assertEqual(result['env_file'],'/review/unsaved.env')
