import unittest,tempfile,pathlib,yaml
from config import normalize_config
class SearchCredentialInheritanceTests(unittest.TestCase):
 def test_search_credentials_do_not_inherit_model_key(self):
  c={'providers':{'modelp':{'type':'openai_compatible','base_url':'https://example.invalid/v1','api_key':'synthetic-model-key'},'exa':{'type':'exa_search','api_key':''},'tavily':{'type':'tavily_search','api_key':''},'brave':{'type':'brave_search','api_key':''}},'models':{'m':{'provider':'modelp','model':'test','capabilities':['chat']}},'profiler':{'ai':{'model':'m'}},'tag_mapping':{'model':'m'}}
  result=normalize_config(c)
  for name in ['exa','tavily','brave']:
   self.assertEqual(result['providers'][name]['api_key'],'',name+' inherited unrelated model credential')
if __name__=='__main__':unittest.main()
