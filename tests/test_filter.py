from datetime import datetime
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault(
    "pixivpy_async",
    types.SimpleNamespace(AppPixivAPI=object),
)
sys.modules.setdefault(
    "aiohttp",
    types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
)
sys.modules.setdefault(
    "aiosqlite",
    types.SimpleNamespace(connect=None),
)

from filter import ContentFilter, calculate_match_score
from pixiv_client import Illust
from tag_categories import TagClassification
from tag_mapping import TagIdentityResolver


class StubTagClassifier:
    async def classify_tags(self, tags):
        return {
            "pantyhose": TagClassification("feature", "ai"),
            "white_hair": TagClassification("feature", "ai"),
            "blue_archive": TagClassification("copyright", "manual"),
            "genshin_impact": TagClassification("copyright", "ai"),
            "high_resolution": TagClassification("non_preference", "ai"),
            "ambiguous_tag": TagClassification("unresolved", "ai"),
        }


class DisplayTagsTests(unittest.IsolatedAsyncioTestCase):
    def test_accepted_aliases_collapse_duplicate_semantic_matches(self):
        item = Illust(
            id=99, title="aliases", user_id=99, user_name="artist",
            tags=["ブルアカ", "blue_archive"], bookmark_count=100, view_count=1000,
            page_count=1, image_urls=["https://example.com/99.jpg"], is_r18=False,
            ai_type=0, create_date=datetime.now(),
        )
        resolver = TagIdentityResolver({"ブルアカ": "blue_archive"})

        score_with_duplicate = calculate_match_score(
            item,
            {"blue_archive": 1.0},
            tag_classifications={"blue_archive": TagClassification("copyright", "manual")},
            tag_resolver=resolver,
        )
        item.tags = ["blue_archive"]
        score_once = calculate_match_score(
            item,
            {"blue_archive": 1.0},
            tag_classifications={"blue_archive": TagClassification("copyright", "manual")},
            tag_resolver=resolver,
        )

        self.assertEqual(score_with_duplicate, score_once)

    async def test_daily_slate_applies_motive_mix_and_identity_caps(self):
        def illust(illust_id, tags):
            return Illust(
                id=illust_id, title=str(illust_id), user_id=illust_id, user_name="artist",
                tags=tags, bookmark_count=1000 - illust_id, view_count=1000,
                page_count=1, image_urls=["https://example.com/a.jpg"], is_r18=False,
                ai_type=0, create_date=datetime.now(),
            )

        illusts = [
            illust(1, ["pantyhose", "blue_archive"]),
            illust(2, ["white_hair", "blue_archive"]),
            illust(3, ["maid", "blue_archive"]),
            illust(4, ["hoshino"]),
            illust(5, ["shiroko"]),
            illust(6, ["genshin_impact"]),
            illust(7, ["fate_grand_order"]),
            illust(8, ["cat_ears"]),
        ]
        classifications = {
            "pantyhose": TagClassification("feature", "manual"),
            "white_hair": TagClassification("feature", "manual"),
            "maid": TagClassification("feature", "manual"),
            "cat_ears": TagClassification("feature", "manual"),
            "blue_archive": TagClassification("copyright", "manual"),
            "hoshino": TagClassification("character", "manual"),
            "shiroko": TagClassification("character", "manual"),
            "genshin_impact": TagClassification("copyright", "manual"),
            "fate_grand_order": TagClassification("copyright", "manual"),
        }
        content_filter = ContentFilter(
            daily_limit=8, max_per_artist=10, exclude_ai=False,
            tag_classifier=StubTagClassifier(),
            daily_slate={"enabled": True, "feature_ratio": 0.55, "character_ratio": 0.15,
                         "copyright_ratio": 0.10, "exploration_ratio": 0.20,
                         "max_per_character": 1, "max_per_copyright": 2},
        )

        with patch("filter.db.get_pushed_ids_batch", new=AsyncMock(return_value=set())), \
             patch("filter.db.get_muted_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_negative_profile", new=AsyncMock(return_value={})), \
             patch("filter.db.get_blocked_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_blocked_artists", new=AsyncMock(return_value=[])), \
             patch.object(content_filter, "_classify_tags_for_illusts", new=AsyncMock(return_value=classifications)):
            ranked = await content_filter.filter(
                illusts,
                xp_profile={tag: 1.0 for tag in classifications},
            )

        self.assertEqual(len(ranked), 7)
        self.assertLessEqual(sum("blue_archive" in item.tags for item in ranked), 2)
        self.assertEqual(sum(getattr(item, "recommendation_motive", None) == "feature" for item in ranked), 3)
    def test_feature_tags_receive_extra_match_weight(self):
        ip_only = Illust(
            id=1,
            title="ip-only",
            user_id=1,
            user_name="artist",
            tags=["genshin_impact"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/a.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )
        feature_match = Illust(
            id=2,
            title="feature-match",
            user_id=2,
            user_name="artist",
            tags=["genshin_impact", "pantyhose"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/b.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        xp_profile = {"top_preference": 5.0, "genshin_impact": 1.0, "pantyhose": 1.0}
        classifications = {
            "genshin_impact": TagClassification("copyright", "manual"),
            "pantyhose": TagClassification("feature", "manual"),
        }

        ip_only_score = calculate_match_score(ip_only, xp_profile, tag_classifications=classifications)
        feature_score = calculate_match_score(feature_match, xp_profile, tag_classifications=classifications)

        self.assertGreater(feature_score, ip_only_score)

    def test_feature_matches_use_diminishing_returns(self):
        three_feature_match = Illust(
            id=20,
            title="three-feature-match",
            user_id=20,
            user_name="artist",
            tags=["pantyhose", "white_hair", "maid"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/20.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )
        four_feature_match = Illust(
            id=21,
            title="four-feature-match",
            user_id=21,
            user_name="artist",
            tags=["pantyhose", "white_hair", "maid", "cat_ears"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/21.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        xp_profile = {
            "top_preference": 5.0,
            "pantyhose": 1.0,
            "white_hair": 0.9,
            "maid": 0.8,
            "cat_ears": 0.7,
        }
        classifications = {
            "pantyhose": TagClassification("feature", "manual"),
            "white_hair": TagClassification("feature", "manual"),
            "maid": TagClassification("feature", "manual"),
            "cat_ears": TagClassification("feature", "manual"),
        }

        three_feature_score = calculate_match_score(
            three_feature_match,
            xp_profile,
            tag_classifications=classifications,
        )
        four_feature_score = calculate_match_score(
            four_feature_match,
            xp_profile,
            tag_classifications=classifications,
        )

        self.assertAlmostEqual(three_feature_score, four_feature_score, places=6)

    async def test_display_tags_feature_first_and_limit_ip_count(self):
        illust = Illust(
            id=1,
            title="test",
            user_id=1,
            user_name="artist",
            tags=["blue_archive", "pantyhose", "genshin_impact", "white_hair"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/a.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        content_filter = ContentFilter(
            daily_limit=10,
            tag_classifier=StubTagClassifier(),
            display_tags_max_ip_count=1,
        )

        await content_filter.apply_display_tags(
            [illust],
            {
                "pantyhose": 0.9,
                "white_hair": 0.8,
                "blue_archive": 1.0,
                "genshin_impact": 0.7,
            },
        )

        self.assertEqual(
            illust.display_tags,
            ["pantyhose", "white_hair", "blue_archive"],
        )

    async def test_display_tags_skips_resolved_non_seed_categories(self):
        illust = Illust(
            id=10,
            title="test",
            user_id=1,
            user_name="artist",
            tags=["pantyhose", "high_resolution", "ambiguous_tag"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/a.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        content_filter = ContentFilter(
            daily_limit=10,
            tag_classifier=StubTagClassifier(),
        )

        await content_filter.apply_display_tags(
            [illust],
            {
                "pantyhose": 1.0,
                "high_resolution": 0.9,
                "ambiguous_tag": 0.8,
            },
        )

        self.assertEqual(illust.display_tags, ["pantyhose"])

    def test_display_tags_max_ip_count_accepts_string_config(self):
        content_filter = ContentFilter(display_tags_max_ip_count="3")

        self.assertEqual(content_filter.display_tags_max_ip_count, 3)

    async def test_ip_diversity_decay_demotes_consecutive_same_ip(self):
        illusts = [
            Illust(
                id=1,
                title="ba-top",
                user_id=1,
                user_name="artist-1",
                tags=["blue_archive", "pantyhose"],
                bookmark_count=100,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/1.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
            Illust(
                id=2,
                title="ba-second",
                user_id=2,
                user_name="artist-2",
                tags=["blue_archive", "white_hair"],
                bookmark_count=100,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/2.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
            Illust(
                id=3,
                title="genshin",
                user_id=3,
                user_name="artist-3",
                tags=["genshin_impact", "white_hair"],
                bookmark_count=100,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/3.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
        ]

        content_filter = ContentFilter(
            daily_limit=10,
            max_per_artist=10,
            exclude_ai=False,
            tag_classifier=StubTagClassifier(),
            ip_diversity={"enabled": True, "decay_factor": 0.6, "floor": 0.1},
        )

        xp_profile = {
            "blue_archive": 1.0,
            "genshin_impact": 0.95,
            "pantyhose": 0.1,
            "white_hair": 0.05,
        }

        with patch("filter.db.get_pushed_ids_batch", new=AsyncMock(return_value=set())), \
             patch("filter.db.get_muted_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_negative_profile", new=AsyncMock(return_value={})), \
             patch("filter.db.get_blocked_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_blocked_artists", new=AsyncMock(return_value=[])):
            ranked = await content_filter.filter(illusts, xp_profile=xp_profile)

        self.assertEqual([illust.id for illust in ranked[:3]], [1, 3, 2])

    async def test_exploration_uses_feature_candidates_outside_normal_top_range(self):
        illusts = [
            Illust(
                id=1,
                title="top-1",
                user_id=1,
                user_name="artist-1",
                tags=["blue_archive", "pantyhose"],
                bookmark_count=1000,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/1.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
            Illust(
                id=2,
                title="top-2",
                user_id=2,
                user_name="artist-2",
                tags=["blue_archive", "white_hair"],
                bookmark_count=900,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/2.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
            Illust(
                id=3,
                title="top-3",
                user_id=3,
                user_name="artist-3",
                tags=["genshin_impact", "pantyhose"],
                bookmark_count=800,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/3.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
            Illust(
                id=4,
                title="normal-top-4",
                user_id=4,
                user_name="artist-4",
                tags=["genshin_impact"],
                bookmark_count=700,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/4.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
            Illust(
                id=5,
                title="feature-explore",
                user_id=5,
                user_name="artist-5",
                tags=["maid"],
                bookmark_count=600,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/5.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
            Illust(
                id=6,
                title="identity-only-explore",
                user_id=6,
                user_name="artist-6",
                tags=["blue_archive"],
                bookmark_count=500,
                view_count=1000,
                page_count=1,
                image_urls=["https://example.com/6.jpg"],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now(),
            ),
        ]

        content_filter = ContentFilter(
            daily_limit=4,
            max_per_artist=10,
            exclude_ai=False,
            exploration_ratio=0.25,
            tag_classifier=StubTagClassifier(),
        )

        xp_profile = {
            "blue_archive": 1.0,
            "genshin_impact": 0.95,
            "pantyhose": 0.7,
            "white_hair": 0.6,
            "maid": 0.55,
        }
        tag_classifications = {
            "blue_archive": TagClassification("copyright", "manual"),
            "genshin_impact": TagClassification("copyright", "manual"),
            "pantyhose": TagClassification("feature", "manual"),
            "white_hair": TagClassification("feature", "manual"),
            "maid": TagClassification("feature", "manual"),
        }

        with patch("filter.db.get_pushed_ids_batch", new=AsyncMock(return_value=set())), \
             patch("filter.db.get_muted_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_negative_profile", new=AsyncMock(return_value={})), \
             patch("filter.db.get_blocked_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_blocked_artists", new=AsyncMock(return_value=[])), \
             patch.object(content_filter, "_classify_tags_for_illusts", new=AsyncMock(return_value=tag_classifications)), \
             patch("random.sample", side_effect=lambda seq, k: list(seq)[:k]), \
             patch("random.shuffle", side_effect=lambda seq: None):
            ranked = await content_filter.filter(illusts, xp_profile=xp_profile)

        self.assertEqual([illust.id for illust in ranked], [1, 2, 3, 5])

    async def test_filter_loads_db_blocked_tags_and_artists(self):
        blocked_tag_illust = Illust(
            id=10,
            title="blocked-tag",
            user_id=10,
            user_name="artist-10",
            tags=["Blue Archive", "pantyhose"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/10.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )
        blocked_artist_illust = Illust(
            id=11,
            title="blocked-artist",
            user_id=99,
            user_name="artist-99",
            tags=["white_hair"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/11.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )
        allowed_illust = Illust(
            id=12,
            title="allowed",
            user_id=12,
            user_name="artist-12",
            tags=["white_hair"],
            bookmark_count=100,
            view_count=1000,
            page_count=1,
            image_urls=["https://example.com/12.jpg"],
            is_r18=False,
            ai_type=0,
            create_date=datetime.now(),
        )

        content_filter = ContentFilter(daily_limit=10, exclude_ai=False)

        with patch("filter.db.get_pushed_ids_batch", new=AsyncMock(return_value=set())), \
             patch("filter.db.get_muted_tags", new=AsyncMock(return_value=[])), \
             patch("filter.db.get_negative_profile", new=AsyncMock(return_value={})), \
             patch("filter.db.get_blocked_tags", new=AsyncMock(return_value=["blue_archive"])), \
             patch("filter.db.get_blocked_artists", new=AsyncMock(return_value=[(99, "artist-99")])):
            ranked = await content_filter.filter(
                [blocked_tag_illust, blocked_artist_illust, allowed_illust],
                xp_profile={"white_hair": 1.0},
            )

        self.assertEqual([illust.id for illust in ranked], [12])
        self.assertIn("blue_archive", content_filter.blacklist_tags)
        self.assertIn(99, content_filter.blocked_artist_ids)


class StubEmbedder:
    def __init__(self):
        self.enabled = True
        self.model = "embed-v1"
        self.semantic_weight = 0.3
        self.embed_tags = AsyncMock(return_value=[1.0, 0.0])

    def cosine_similarity(self, left, right):
        return 1.0

    def normalize_similarity(self, similarity):
        return (similarity + 1.0) / 2.0


class UserEmbeddingRefreshTests(unittest.IsolatedAsyncioTestCase):
    def _illust(self, illust_id):
        return Illust(
            id=illust_id, title=str(illust_id), user_id=illust_id, user_name="artist",
            tags=["white_hair"], bookmark_count=100, view_count=1000,
            page_count=1, image_urls=["https://example.com/a.jpg"], is_r18=False,
            ai_type=0, create_date=datetime.now(),
        )

    def _build_filter(self, embedder):
        return ContentFilter(
            daily_limit=5, max_per_artist=10, exclude_ai=False,
            tag_classifier=StubTagClassifier(), embedder=embedder,
        )

    def _patch_db(self, content_filter, **overrides):
        from unittest.mock import patch as _patch

        patches = [
            _patch("filter.db.get_pushed_ids_batch", new=AsyncMock(return_value=set())),
            _patch("filter.db.get_muted_tags", new=AsyncMock(return_value=[])),
            _patch("filter.db.get_negative_profile", new=AsyncMock(return_value={})),
            _patch("filter.db.get_blocked_tags", new=AsyncMock(return_value=[])),
            _patch("filter.db.get_blocked_artists", new=AsyncMock(return_value=[])),
            _patch(
                "filter.db.get_illust_embeddings_batch",
                new=AsyncMock(return_value={1: [1.0, 0.0]}),
            ),
            _patch.object(
                content_filter, "_classify_tags_for_illusts",
                new=AsyncMock(return_value={
                    "white_hair": TagClassification("feature", "manual"),
                }),
            ),
        ]
        patches.extend(overrides.get("extra", []))
        return patches

    async def test_stale_model_cache_is_refreshed_with_current_model(self):
        from embedder import profile_embedding_hash

        xp_profile = {"white_hair": 2.0}
        embedder = StubEmbedder()
        content_filter = self._build_filter(embedder)
        save = AsyncMock()
        # 缓存的画像向量来自旧模型（profile_hash 匹配但 model 不匹配）
        stale_cache = AsyncMock(return_value=None)
        patches = self._patch_db(
            content_filter,
            extra=[
                patch("filter.db.get_current_user_embedding", new=stale_cache),
                patch("filter.db.save_user_embedding", new=save),
            ],
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        await content_filter.filter([self._illust(1)], xp_profile=xp_profile, user_id=7)

        stale_cache.assert_called_once_with(7, "embed-v1", profile_embedding_hash(xp_profile))
        embedder.embed_tags.assert_awaited_once()
        save.assert_awaited_once_with(
            7, [1.0, 0.0], "embed-v1", profile_embedding_hash(xp_profile)
        )

    async def test_current_model_cache_is_reused_without_recompute(self):
        embedder = StubEmbedder()
        content_filter = self._build_filter(embedder)
        save = AsyncMock()
        current_cache = unittest.mock.Mock(return_value=[1.0, 0.0])
        patches = self._patch_db(
            content_filter,
            extra=[
                patch("filter.db.get_current_user_embedding", new=current_cache),
                patch("filter.db.save_user_embedding", new=save),
            ],
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        await content_filter.filter(
            [self._illust(1)], xp_profile={"white_hair": 2.0}, user_id=7
        )

        current_cache.assert_called_once()
        embedder.embed_tags.assert_not_awaited()
        save.assert_not_awaited()
