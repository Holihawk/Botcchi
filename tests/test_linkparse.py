"""链接解析：B站/抖音解析器、冷却、被动接入、parse_link 与定时调度工具。"""

import json

import httpx
import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.linkparse import COOLDOWN_SECONDS, LinkParseService, ParseError
from botcchi.linkparse.base import format_duration, format_num, truncate


@pytest.fixture(autouse=True)
def _reset_platform_caches():
    """清空进程级设备指纹/访客态缓存，保证各用例握手计数独立。"""
    from botcchi.linkparse.bilibili import api as bili_api
    from botcchi.linkparse.weibo import auth as weibo_auth

    bili_api._buvid = {}
    bili_api._wbi_keys = None
    weibo_auth._state.clear()
    yield


BILI_VIEW = {
    "code": 0,
    "data": {
        "bvid": "BV1xx411c7mD",
        "cid": 123456,
        "title": "【测试】孤独摇滚名场面",
        "desc": "波奇酱的吉他solo合集，值得一看",
        "pic": "https://i0.hdslb.com/cover.jpg",
        "duration": 754,
        "owner": {"name": "小孤独", "mid": 1},
        "stat": {
            "view": 1234567, "danmaku": 4521,
            "like": 89000, "favorite": 32100,
        },
    },
}

BILI_MULTI_PAGE = {
    "code": 0,
    "data": {
        "bvid": "BV11Aga6oENR",
        "cid": 9001,
        "title": "角色展示",
        "desc": "多语言配音合集",
        "pic": "https://i0.hdslb.com/multi.jpg",
        "duration": 940,
        "owner": {"name": "绝区零", "mid": 2},
        "stat": {"view": 1794000, "danmaku": 9122, "like": 126000, "favorite": 32000},
        "pages": [
            {"cid": 9001, "part": "中配", "duration": 314},
            {"cid": 9002, "part": "日配", "duration": 315, "first_frame": "https://i0.hdslb.com/p2.jpg"},
            {"cid": 9003, "part": "英配", "duration": 311},
        ],
    },
}

BILI_DYNAMIC_DRAW = {
    "code": 0,
    "data": {
        "item": {
            "id_str": "1225943683926327330",
            "type": "DYNAMIC_TYPE_DRAW",
            "visible": True,
            "basic": {
                "comment_type": 11,
                "jump_url": "//www.bilibili.com/opus/1225943683926327330",
            },
            "modules": {
                "module_author": {"name": "七海Nana7mi", "mid": 434334701},
                "module_stat": {
                    "like": {"count": 1728},
                    "comment": {"count": 190},
                    "forward": {"count": 4161},
                },
                "module_dynamic": {
                    "desc": None,
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {
                            "title": None,
                            "pics": [
                                {"url": "https://i0.hdslb.com/dyn/a.png"},
                                {"url": "https://i0.hdslb.com/dyn/b.png"},
                            ],
                            "summary": {
                                "text": "生日快乐",
                                "rich_text_nodes": [
                                    {
                                        "type": "RICH_TEXT_NODE_TYPE_TEXT",
                                        "text": "生日快乐",
                                    },
                                    {
                                        "type": "RICH_TEXT_NODE_TYPE_EMOJI",
                                        "text": "[星星眼]",
                                    },
                                ],
                            },
                        },
                    },
                },
            },
        }
    },
}

# 长图文：web-dynamic detail 里 summary 被截断（has_more=True），只给预览
BILI_DYNAMIC_LONG_OPUS = {
    "code": 0,
    "data": {
        "item": {
            "id_str": "1300000000000000001",
            "type": "DYNAMIC_TYPE_DRAW",
            "visible": True,
            "basic": {"comment_type": 12,
                      "jump_url": "//www.bilibili.com/opus/1300000000000000001"},
            "modules": {
                "module_author": {"name": "图文作者"},
                "module_stat": {"like": {"count": 100}},
                "module_dynamic": {
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {
                            "title": "长篇图文",
                            "pics": [{"url": "https://i0.hdslb.com/preview.png"}],
                            "summary": {
                                "text": "开头预览……",
                                "has_more": True,
                                "rich_text_nodes": [
                                    {"type": "RICH_TEXT_NODE_TYPE_TEXT",
                                     "text": "开头预览……"}
                                ],
                            },
                        },
                    }
                },
            },
        }
    },
}

# opus/detail：完整分段正文（文字/图片交替 + 代码块），modules 为列表
BILI_OPUS_DETAIL = {
    "code": 0,
    "data": {
        "item": {
            "id_str": "1300000000000000001",
            "modules": [
                {
                    "module_content": {
                        "paragraphs": [
                            {"para_type": 1, "text": {"nodes": [
                                {"word": {"words": "第一段正文"}},
                                {"rich": {"text": "带链接", "jump_url": "//x"}},
                            ]}},
                            {"para_type": 3, "line": {}},  # 分割线：跳过
                            {"para_type": 2, "pic": {"pics": [
                                {"url": "https://i0.hdslb.com/full-1.png"},
                                {"url": "https://i0.hdslb.com/full-2.png"},
                            ]}},
                            {"para_type": 1, "text": {"nodes": [
                                {"word": {"words": "第二段正文"}},
                            ]}},
                            {"para_type": 7, "code": {
                                "lang": "language-python", "content": "print(1)"}},
                            {"para_type": 2, "pic": {"pics": [
                                {"url": "https://i0.hdslb.com/full-3.png"},
                            ]}},
                        ]
                    }
                }
            ],
        }
    },
}

BILI_DYNAMIC_FORWARD = {
    "code": 0,
    "data": {
        "item": {
            "id_str": "1225245957853020192",
            "type": "DYNAMIC_TYPE_FORWARD",
            "visible": True,
            "basic": {"comment_type": 17},
            "modules": {
                "module_author": {"name": "七海Nana7mi", "mid": 434334701},
                "module_stat": {
                    "like": {"count": 483},
                    "comment": {"count": 29},
                    "forward": {"count": 1},
                },
                "module_dynamic": {
                    "desc": {
                        "text": "恭喜中奖",
                        "rich_text_nodes": [
                            {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": "恭喜中奖"}
                        ],
                    },
                    "major": None,
                },
            },
            "orig": {
                "id_str": "1200000000000000000",
                "type": "DYNAMIC_TYPE_AV",
                "visible": True,
                "modules": {
                    "module_author": {"name": "小问号", "mid": 437270664},
                    "module_dynamic": {
                        "major": {
                            "type": "MAJOR_TYPE_ARCHIVE",
                            "archive": {
                                "bvid": "BV1xx411c7mD",
                                "title": "驴马杀",
                                "cover": "https://i0.hdslb.com/orig.jpg",
                            },
                        }
                    },
                },
            },
        }
    },
}

BILI_DYNAMIC_AV = {
    "code": 0,
    "data": {
        "item": {
            "id_str": "1228106449050140729",
            "type": "DYNAMIC_TYPE_AV",
            "visible": True,
            "basic": {"comment_type": 1},
            "modules": {
                "module_author": {"name": "小孤独", "mid": 1},
                "module_stat": {},
                "module_dynamic": {
                    "major": {
                        "type": "MAJOR_TYPE_ARCHIVE",
                        "archive": {"bvid": "BV1xx411c7mD", "title": "孤独摇滚"},
                    }
                },
            },
        }
    },
}

BILI_DYNAMIC_LIVE = {
    "code": 0,
    "data": {
        "item": {
            "id_str": "1228642821717622800",
            "type": "DYNAMIC_TYPE_LIVE_RCMD",
            "visible": True,
            "basic": {"comment_type": 17},
            "modules": {
                "module_author": {"name": "七海Nana7mi", "mid": 434334701},
                "module_stat": {},
                "module_dynamic": {
                    "major": {
                        "type": "MAJOR_TYPE_LIVE_RCMD",
                        "live_rcmd": {
                            "content": json.dumps(
                                {"live_play_info": {"room_id": 21452505}}
                            )
                        },
                    }
                },
            },
        }
    },
}

BILI_LIVE_ROOM = {
    "code": 0,
    "data": {
        "uid": 434334701,
        "room_id": 21452505,
        "title": "二次元的偷",
        "live_status": 1,
        "online": 273000,
        "area_name": "主机游戏",
        "parent_area_name": "单机游戏",
        "tags": "七海,海子姐",
        "user_cover": "https://i0.hdslb.com/live/cover.jpg",
        "keyframe": "https://i0.hdslb.com/live/keyframe.jpg",
    },
}

BILI_LIVE_MASTER = {
    "code": 0,
    "data": {"info": {"uname": "七海Nana7mi"}, "follower_num": 1111412},
}

BILI_ARTICLE = {
    "code": 0,
    "data": {
        "title": "未知的光",
        "author_name": "健行见远渐忘",
        "mid": 91221505,
        "banner_url": "https://i0.hdslb.com/article/banner.jpg",
        "origin_image_urls": ["https://i0.hdslb.com/article/1.jpg"],
        "stats": {"view": 1669352, "like": 32830, "favorite": 17680, "reply": 13955},
    },
}

BILI_FAVLIST = {
    "code": 0,
    "data": {
        "info": {
            "title": "吉他教学",
            "cover": "https://i0.hdslb.com/fav.jpg",
            "intro": "练琴用",
            "media_count": 3,
            "cnt_info": {"collect": 120, "play": 45000},
            "upper": {"mid": 1, "name": "小孤独"},
        },
        "medias": [
            {"title": "第一课", "cover": "https://i0.hdslb.com/m1.jpg"},
            {"title": "第二课", "cover": "https://i0.hdslb.com/m2.jpg"},
            {"title": "第三课", "cover": "https://i0.hdslb.com/m3.jpg"},
        ],
    },
}

DOUYIN_ROUTER = {
    "loaderData": {
        "video_(id)/page": {
            "videoInfoRes": {
                "item_list": [
                    {
                        "aweme_id": "7521023890996514083",
                        "desc": "吉他英雄的日常练习",
                        "author": {"nickname": "后藤一里"},
                        "statistics": {
                            "digg_count": 15000,
                            "comment_count": 800,
                            "share_count": 300,
                            "collect_count": 2100,
                        },
                        "video": {
                            "play_addr": {"url_list": ["https://v.example/1.mp4"]},
                            "cover": {"url_list": ["https://p.example/cover.jpg"]},
                            "duration": 33500,
                        },
                    }
                ]
            }
        }
    }
}

# 图文（note）：一张普通图 + 一张 Live Photo（带 video 字段，仍取静图）
DOUYIN_NOTE_ROUTER = {
    "loaderData": {
        "note_(id)/page": {
            "videoInfoRes": {
                "item_list": [
                    {
                        "aweme_id": "7469411074119322899",
                        "desc": "秋日碎片",
                        "author": {"nickname": "喜多郁代"},
                        "statistics": {
                            "digg_count": 6000,
                            "comment_count": 120,
                            "share_count": 40,
                            "collect_count": 900,
                        },
                        "images": [
                            {"url_list": ["https://p.example/img1.jpg"]},
                            {
                                "url_list": ["https://p.example/img2.jpg"],
                                "video": {
                                    "play_addr": {"url_list": ["https://v.example/live.mp4"]},
                                    "cover": {"url_list": ["https://p.example/img2.jpg"]},
                                    "duration": 3000,
                                },
                            },
                        ],
                    }
                ]
            }
        }
    }
}


def caesar(text: str) -> str:
    """还原方向为 -1，构造夹具时用 +1 混淆 key。"""
    return "".join(chr((ord(c) + 1) % 256) for c in text)


KUAISHOU_STATE = {
    caesar("/rest/wd/ugH5App/photo/simple/info"): {
        "info": {
            "photo": {
                "photoId": "3xkuaishou123",
                "caption": "快手上的猫猫",
                "userName": "快手用户ㅤ",
                "viewCount": 50000,
                "likeCount": 3000,
                "commentCount": 120,
                "duration": 15000,
                "coverUrls": [{"url": "https://ks.example/cover.jpg"}],
                "mainMvUrls": [{"url": "https://ks.example/video.mp4"}],
                "ext_params": {},
            }
        }
    }
}

REDNOTE_STATE = {
    "noteData": {
        "data": {
            "noteData": {
                "title": "秋日穿搭分享",
                "desc": "很喜欢的一套",
                "user": {"nickName": "小红薯", "avatar": "", "userId": "u1"},
                "lastUpdateTime": 1730000000000,
                "interactInfo": {
                    "likedCount": "12000",
                    "collectedCount": "800",
                    "commentCount": "56",
                    "shareCount": "10",
                },
                "cover": {"fileId": "cover-file-id"},
                "imageList": [
                    {"fileId": "img-1"},
                    {"fileId": "img-2"},
                    {"fileId": "img-3"},
                ],
            }
        }
    }
}

# 图集博文（走 weibo.com/ajax/statuses/show）
WEIBO_SHOW = {
    "user": {"screen_name": "微博用户", "idstr": "7207262816"},
    "idstr": "5181502771168068",
    "mblogid": "Pycg54TRO",
    "text_raw": "今天天气真不错 http://t.cn/Adead​",
    "isLongText": False,
    "reposts_count": 10,
    "comments_count": 5,
    "attitudes_count": 100,
    "pic_num": 2,
    "pic_ids": ["p1", "p2"],
    "pic_infos": {
        "p1": {"type": "pic", "large": {"url": "https://wx.sinaimg.cn/large/1.jpg"}},
        # livephoto 也当普通图片取
        "p2": {"type": "livephoto", "large": {"url": "https://wx.sinaimg.cn/large/2.jpg"}},
    },
}

# 长文视频转发博文
WEIBO_SHOW_LONG = {
    "user": {"screen_name": "长文君", "idstr": "123"},
    "idstr": "5234367615996775",
    "mblogid": "P5kWdcfDe",
    "text_raw": "开头 http://t.cn/short​",
    "isLongText": True,
    "reposts_count": 30,
    "comments_count": 2,
    "attitudes_count": 21,
    "pic_num": 0,
    "page_info": {
        "type": "video",
        "object_type": "video",
        "page_pic": "https://wx.sinaimg.cn/video-cover.jpg",
        "media_info": {"stream_url_hd": "https://f.video.weibocdn.com/hd.mp4", "duration": 14},
    },
    "retweeted_status": {
        "user": {"screen_name": "原博主"},
        "text_raw": "被转发的原文内容",
    },
}

WEIBO_LONGTEXT = {"data": {"longTextContent_raw": "这是被展开的完整长文正文，很长很长。"}}

WEIBO_ARTICLE = {
    "data": {
        "title": "小米14的存储扩容",
        "userinfo": {"screen_name": "小米澎湃OS"},
        "read_count": "19万+",
        "summary": "关于随机小礼品的说明",
        "target_url": "https://card.weibo.com/article/m/show/id/2309404962180771742222",
        "cover_img": {"image": {"url": "https://wx.sinaimg.cn/large/article-cover.jpg"}},
    }
}

NCM_SONG = {
    "name": "青春コンプレックス",
    "singer": "結束バンド",
    "duration": "3:45",
    "picimg": "https://p1.music.126.net/song-cover.jpg",
}


ZHIHU_ARTICLE = {
    "id": 584852445,
    "type": "article",
    "title": "打开链接发现要提取码怎么办",
    "created": 1668834470,
    "updated": 1744356078,
    "excerpt": '<img src="https://picx.zhimg.com/v2-x.jpg"/>正文节选内容在这里',
    "content": '<p>正文</p><img data-original="https://pic2.zhimg.com/v2-body.jpg"/>',
    "title_image": "https://pic1.zhimg.com/v2-title.jpg",
    "voteup_count": 62,
    "comment_count": 41,
    "ip_info": "湖南",
    "author": {"name": "我呀", "headline": "", "url_token": "wo-ya-97-41"},
}

ZHIHU_QUESTION = {
    "id": 19550225,
    "type": "question",
    "title": "如何正确使用知乎？",
    "detail": "<p>这是问题的详细描述正文。</p>",
    "excerpt": "问题摘要",
    "visit_count": 2814661,
    "answer_count": 1489,
    "follower_count": 16130,
    "comment_count": 175,
    "author": {"name": "胡维", "headline": "前知乎员工", "url_token": "whale"},
}

ZHIHU_ANSWER = {
    "id": 315796660,
    "type": "answer",
    "created_time": 1518198081,
    "updated_time": 1519054975,
    "content": '<p>回答正文</p><img data-actualsrc="https://pica.zhimg.com/v2-ans.jpg"/>',
    "excerpt": "这边半个月前刚云游南方回来，全程女装。",
    "voteup_count": 2903,
    "comment_count": 526,
    "thanks_count": 456,
    "question": {"id": "67423622", "title": "怎么女装过安检？", "type": "question"},
    "author": {"name": "小六花", "headline": "喜欢穿小裙子", "url_token": "reimuhakurei2017"},
}


def _tieba_res(
    *, tid, title, fname="genshin", reply=1, view=0, share=0, agree=0,
    author="楼主甲", frags=(), video=None, errorno=0,
):
    """就地构建贴吧移动端 protobuf 响应字节，供 MockTransport 回放。"""
    from botcchi.linkparse.tieba import _message

    res = _message("PbPageResIdl")()
    if errorno:
        res.error.errorno = errorno
        res.error.errmsg = "贴子可能已被删除"
        return res.SerializeToString()
    res.data.forum.name = fname
    th = res.data.thread
    th.id, th.title, th.reply_num = tid, title, reply
    th.view_num, th.share_num, th.agree.agree_num = view, share, agree
    th.author.name_show = author
    if video:
        th.video_info.video_url, th.video_info.thumbnail_url, th.video_info.video_duration = video
    post = res.data.post_list.add()
    post.floor = 1
    for ftype, text, extra in frags:
        c = post.content.add()
        c.type = ftype
        if ftype in (3, 20):
            c.origin_src = extra
        else:
            c.text = text
            if ftype == 1:
                c.link = extra
    return res.SerializeToString()


TIEBA_TEXT = _tieba_res(
    tid=9451359896, title="原神藤编八奇活动", fname="genshin", reply=1, author="琴非瑟",
    frags=[(0, "活动正文 ", ""), (4, "某人", ""), (1, "act.mihoyo.com", "https://act.mihoyo.com/e")],
)
TIEBA_IMAGE = _tieba_res(
    tid=10855044566, title="史尔特尔妈妈", fname="史尔特尔", reply=3, agree=3, author="鸿雪",
    frags=[(11, "😍", ""), (3, "", "https://tiebapic.baidu.com/a.jpg"),
           (20, "", "https://tiebapic.baidu.com/b.jpg")],
)
TIEBA_VIDEO = _tieba_res(
    tid=8000000001, title="视频帖", fname="test", reply=7, view=88000, author="视频君",
    video=("https://vid.tb/v.mp4", "https://vid.tb/cover.jpg", 30), frags=[(0, "看视频", "")],
)
TIEBA_DELETED = _tieba_res(tid=9101148197, title="", errorno=4)


def _x_node(rid, name, screen, text, **extra):
    """构造 easycomment.ai 时间线里的一条推文节点。"""
    legacy = {
        "full_text": text,
        "display_text_range": [0, len(text)],
        "favorite_count": extra.get("favorite_count", 0),
        "reply_count": extra.get("reply_count", 0),
        "retweet_count": extra.get("retweet_count", 0),
        "quote_count": extra.get("quote_count", 0),
        "bookmark_count": extra.get("bookmark_count", 0),
    }
    if "extended_entities" in extra:
        legacy["extended_entities"] = extra["extended_entities"]
    node = {
        "__typename": "Tweet",
        "rest_id": rid,
        "core": {"user_results": {"result": {"core": {"name": name, "screen_name": screen}}}},
        "legacy": legacy,
    }
    if "views" in extra:  # 老推文可能没有 views.count
        node["views"] = extra["views"]
    if "quoted" in extra:
        node["quoted_status_result"] = {"result": extra["quoted"]}
    return node


def _x_envelope(*nodes):
    entries = [
        {
            "content": {
                "__typename": "TimelineTimelineItem",
                "itemContent": {
                    "__typename": "TimelineTweet",
                    "tweet_results": {"result": n},
                },
            }
        }
        for n in nodes
    ]
    return {
        "code": 100000,
        "message": "Request Success",
        "data": {
            "data": {
                "threaded_conversation_with_injections_v2": {
                    "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
                }
            }
        },
    }


X_VIDEO_QUOTED = _x_envelope(
    _x_node(
        "1900000000000000001",
        "主播娘",
        "vtuber_x",
        "看这个视频！ https://t.co/abcd",
        favorite_count=13000,
        reply_count=7,
        retweet_count=1000,
        quote_count=426,
        views={"count": "170156"},
        extended_entities={
            "media": [
                {
                    "type": "video",
                    "media_url_https": "https://pbs.twimg.com/amplify_video_thumb/1/img/x.jpg",
                    "video_info": {
                        "duration_millis": 6281,
                        "variants": [
                            {"content_type": "application/x-mpegURL", "url": "https://v/x.m3u8"},
                            {"content_type": "video/mp4", "bitrate": 432000, "url": "https://v/lo.mp4"},
                            {"content_type": "video/mp4", "bitrate": 1280000, "url": "https://v/hi.mp4"},
                        ],
                    },
                }
            ]
        },
        quoted=_x_node(
            "1900000000000000099",
            "被引用者",
            "orig_user",
            "这是被引用的原推文内容",
            favorite_count=5,
        ),
    )
)

# 老推文：views 只有 state 没有 count，stats 不应崩且省略浏览数
X_NO_VIEWS = _x_envelope(
    _x_node(
        "20",
        "jack",
        "jack",
        "just setting up my twttr",
        favorite_count=307472,
        reply_count=17959,
        retweet_count=124855,
        views={"state": "Enabled"},
    )
)

# 推文不存在（被删/不可见）→ TweetTombstone，索引里取不到该 id
X_TOMBSTONE = {
    "code": 100000,
    "message": "Request Success",
    "data": {
        "data": {
            "threaded_conversation_with_injections_v2": {
                "instructions": [
                    {
                        "type": "TimelineAddEntries",
                        "entries": [
                            {
                                "content": {
                                    "__typename": "TimelineTimelineItem",
                                    "itemContent": {
                                        "__typename": "TimelineTweet",
                                        "tweet_results": {
                                            "result": {"__typename": "TweetTombstone"}
                                        },
                                    },
                                }
                            }
                        ],
                    }
                ]
            }
        }
    },
}


def make_service(**settings_kw) -> LinkParseService:
    from botcchi.config import LinkParseSettings

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        if "b23.tv" in url:
            return httpx.Response(
                302,
                headers={
                    "Location": "https://www.bilibili.com/video/BV1xx411c7mD?p=1"
                },
            )
        if "frontend/finger/spi" in url:
            return httpx.Response(
                200, json={"code": 0, "data": {"b_3": "buvid3-x", "b_4": "buvid4-x"}}
            )
        if "api.bilibili.com/x/web-interface/nav" in url:
            return httpx.Response(
                200,
                json={
                    "code": -101,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/wbi/" + "a" * 32 + ".png",
                            "sub_url": "https://i0.hdslb.com/wbi/" + "b" * 32 + ".png",
                        }
                    },
                },
            )
        if "api.bilibili.com/x/web-interface/view" in url:
            cookie = request.headers.get("Cookie", "")
            assert "buvid3=buvid3-x" in cookie and "buvid4=buvid4-x" in cookie
            if request.url.params.get("bvid") == "BV11Aga6oENR":
                return httpx.Response(200, json=BILI_MULTI_PAGE)
            assert request.url.params.get("bvid") == "BV1xx411c7mD"
            return httpx.Response(200, json=BILI_VIEW)
        if "web-dynamic/v1/opus/detail" in url:
            # 长图文取完整正文的接口
            body = {"1300000000000000001": BILI_OPUS_DETAIL}.get(
                request.url.params.get("id")
            )
            return httpx.Response(200, json=body or {"code": -404})
        if "web-dynamic/v1/detail" in url:
            # 动态接口必须带上设备指纹与 WBI 签名，否则线上会被风控
            cookie = request.headers.get("Cookie", "")
            assert "buvid3=buvid3-x" in cookie and "buvid4=buvid4-x" in cookie
            assert request.url.params.get("w_rid")
            dynamics = {
                "1225943683926327330": BILI_DYNAMIC_DRAW,
                "1225245957853020192": BILI_DYNAMIC_FORWARD,
                "1228106449050140729": BILI_DYNAMIC_AV,
                "1228642821717622800": BILI_DYNAMIC_LIVE,
                "1300000000000000001": BILI_DYNAMIC_LONG_OPUS,
            }
            body = dynamics.get(request.url.params.get("id"))
            return httpx.Response(200, json=body or {"code": -404, "message": "不存在"})
        if "live.bilibili.com/room/v1/Room/get_info" in url:
            return httpx.Response(200, json=BILI_LIVE_ROOM)
        if "live.bilibili.com/live_user/v1/Master/info" in url:
            return httpx.Response(200, json=BILI_LIVE_MASTER)
        if "api.bilibili.com/x/article/viewinfo" in url:
            return httpx.Response(200, json=BILI_ARTICLE)
        if "api.bilibili.com/x/v3/fav/resource/list" in url:
            assert request.url.params.get("media_id") == "3117418552"
            return httpx.Response(200, json=BILI_FAVLIST)
        if "api.bilibili.com/x/player/playurl" in url:
            assert request.headers.get("Referer") == "https://www.bilibili.com/"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "durl": [
                            {
                                "url": "https://cdn.example/bili.mp4",
                                "size": 5 * 1024 * 1024,
                            }
                        ]
                    },
                },
            )
        if "cdn.example/bili.mp4" in url:
            return httpx.Response(200, content=b"BILIVIDEO")
        if "v.douyin.com" in url:
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://www.iesdouyin.com/share/video/"
                        "7521023890996514083/?region=CN"
                    )
                },
            )
        if "jx.douyin.com" in url:
            return httpx.Response(
                302,
                headers={
                    "Location": "https://www.douyin.com/note/7469411074119322899"
                },
            )
        if "m.douyin.com/share/" in url:
            router = (
                DOUYIN_NOTE_ROUTER
                if ("/note/" in url or "/slides/" in url)
                else DOUYIN_ROUTER
            )
            html = (
                "<html><script>window._ROUTER_DATA = "
                + json.dumps(router, ensure_ascii=False)
                + "</script></html>"
            )
            return httpx.Response(200, text=html)
        if "visitor.passport.weibo.cn/visitor/genvisitor2" in url:
            return httpx.Response(
                200,
                text='visitor_gray_callback({"retcode":20000000,"data":'
                '{"sub":"SUBx","subp":"SUBPx"}})',
            )
        if url.rstrip("/") == "https://www.weibo.com":
            return httpx.Response(
                200, headers={"Set-Cookie": "XSRF-TOKEN=xsrf-test; Path=/"}
            )
        if "weibo.com/ajax/statuses/show" in url:
            # 访客态头必须带上，否则线上会 302
            assert request.headers.get("X-Xsrf-Token") == "xsrf-test"
            assert "SUB=SUBx" in request.headers.get("Cookie", "")
            wid = request.url.params.get("id")
            body = WEIBO_SHOW_LONG if wid in ("5234367615996775", "P5kWdcfDe") else WEIBO_SHOW
            return httpx.Response(200, json=body)
        if "weibo.com/ajax/statuses/longtext" in url:
            return httpx.Response(200, json=WEIBO_LONGTEXT)
        if "card.weibo.com/article/m/aj/detail" in url:
            return httpx.Response(200, json=WEIBO_ARTICLE)
        if "xhslink.com" in url:
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://www.xiaohongshu.com/discovery/item/"
                        "66aabbcc000000001e03b8f2?xsec_token=ABtoken="
                    )
                },
            )
        if "xiaohongshu.com/discovery/item" in url:
            # 失效 token 会被重定向到 /404/ 安全页
            if "EXPIRED" in url:
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://www.xiaohongshu.com/404/sec_x?source=xhs"
                    },
                )
            html = (
                "<html><script>window.__INITIAL_STATE__="
                + json.dumps(REDNOTE_STATE, ensure_ascii=False)
                + "</script></html>"
            )
            return httpx.Response(200, text=html)
        if "xiaohongshu.com/404/" in url:
            return httpx.Response(200, text="<html>安全中心</html>")
        if "v.kuaishou.com" in url or "www.kuaishou.com" in url:
            return httpx.Response(
                302,
                headers={
                    "Location": "https://v.m.chenzhongtech.com/fw/photo/3xkuaishou123"
                },
            )
        if "chenzhongtech.com/fw/photo" in url or "m.gifshow.com/fw/photo" in url:
            html = (
                "<html><script>window.INIT_STATE = "
                + json.dumps(KUAISHOU_STATE, ensure_ascii=False)
                + "</script></html>"
            )
            return httpx.Response(200, text=html)
        if "tiebac.baidu.com/c/f/pb/page" in url:
            from botcchi.linkparse.tieba import _message

            assert request.headers.get("User-Agent") == "miku/39"
            assert request.headers.get("x_bd_data_type") == "protobuf"
            # 从 multipart 体里剥出 protobuf 段，解析出 tid(kz)
            blob = request.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            req = _message("PbPageReqIdl")()
            req.ParseFromString(blob)
            body = {
                9451359896: TIEBA_TEXT,
                10855044566: TIEBA_IMAGE,
                8000000001: TIEBA_VIDEO,
                9101148197: TIEBA_DELETED,
            }.get(req.data.kz, TIEBA_DELETED)
            return httpx.Response(200, content=body)
        if "www.zhihu.com/api/v4/articles/" in url:
            assert request.headers.get("x-zse-96", "").startswith("2.0_")
            return httpx.Response(200, json=ZHIHU_ARTICLE)
        if "www.zhihu.com/api/v4/questions/" in url:
            assert request.headers.get("x-zse-96", "").startswith("2.0_")
            return httpx.Response(200, json=ZHIHU_QUESTION)
        if "api.zhihu.com/answers/" in url:
            # 移动端接口靠匿名 OAuth 令牌，不走 zse96 签名
            assert request.headers.get("Authorization", "").startswith("oauth ")
            return httpx.Response(200, json=ZHIHU_ANSWER)
        if "easycomment.ai/api/twitter" in url:
            body = json.loads(request.content)
            payload = {
                "1900000000000000001": X_VIDEO_QUOTED,
                "20": X_NO_VIEWS,
                "1900000000000000404": X_TOMBSTONE,
            }.get(body.get("pid"), {"code": 100404, "message": "not found"})
            return httpx.Response(200, json=payload)
        if url.endswith("/api/key"):
            import base64

            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "key": base64.b64encode(b"0" * 16).decode(),
                        "keyId": "kid",
                        "keyToken": "ktok",
                    },
                },
            )
        if url.endswith("/api/getSongInfo"):
            return httpx.Response(200, json={"code": 200, "data": NCM_SONG})
        if url.endswith("/api/getSongUrl"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {"url": "https://m7.music.126.net/song.mp3"},
                },
            )
        if "song.mp3" in url:
            return httpx.Response(200, content=b"MP3AUDIO")
        if (
            "sinaimg.cn" in url
            or "ks.example" in url
            or "cover" in url
            or "hdslb.com" in url
        ):
            return httpx.Response(200, content=b"\x89PNGfake")
        return httpx.Response(404)

    service = LinkParseService(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        settings=LinkParseSettings(**settings_kw),
    )
    service.requests = requests  # type: ignore[attr-defined]  测试断言请求细节用
    return service


class ImageChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.images: list[str] = []

    async def send_image(self, session, file):
        self.images.append(file)


class MediaChannel(ImageChannel):
    def __init__(self):
        super().__init__()
        self.videos: list[str] = []
        self.records: list[str] = []
        self.forwards: list[list[tuple[str, str, str | None]]] = []

    async def send_video(self, session, file):
        self.videos.append(file)

    async def send_record(self, session, file):
        self.records.append(file)

    async def send_forward(self, session, nodes):
        self.forwards.append(list(nodes))


def group_msg(text: str, chat_id="777", at_me=False) -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id=chat_id),
        user_id="10001",
        user_name="hhk",
        text=text,
        at_me=at_me,
    )


def test_format_helpers():
    assert format_num(9999) == "9999"
    assert format_num(1234567) == "123.5万"
    assert format_num(120000) == "12万"
    assert format_num(250_000_000) == "2.5亿"
    assert format_duration(754) == "12:34"
    assert format_duration(3723) == "1:02:03"
    assert truncate("a" * 100, 10) == "a" * 9 + "…"


async def test_bilibili_link_and_bare_bv():
    service = make_service()
    link = await service.parse_text(
        "看这个 https://www.bilibili.com/video/BV1xx411c7mD 太强了"
    )
    assert link.platform == "B站" and "孤独摇滚" in link.title
    assert link.author == "小孤独" and link.duration == "12:34"
    assert "播放 123.5万" in link.stats and "弹幕 4521" in link.stats
    assert link.url == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert link.cover == "https://i0.hdslb.com/cover.jpg"

    # 裸 BV 号（整条消息只有号）
    assert (await service.parse_text("BV1xx411c7mD")).title == BILI_VIEW["data"]["title"]
    # 无链接
    assert await service.parse_text("今天天气不错") is None
    await service.aclose()


async def test_bilibili_multi_page():
    service = make_service()
    link = await service.parse_text("https://www.bilibili.com/video/BV11Aga6oENR?p=3")
    assert link.title == "角色展示 | P3 英配" and link.duration == "5:11"
    assert link.url == "https://www.bilibili.com/video/BV11Aga6oENR?p=3"
    # 分P 的视频流要取该P的 cid
    playurl = [r for r in service.requests if "playurl" in str(r.url)][-1]
    assert playurl.url.params.get("cid") == "9003"

    # 裸号后跟页码，且分P自带首帧图时优先用它
    link = await service.parse_text("BV11Aga6oENR 2")
    assert "P2 日配" in link.title and link.cover == "https://i0.hdslb.com/p2.jpg"

    # 越界页码退回末页，不越界的单P视频不加分P后缀
    link = await service.parse_text("https://www.bilibili.com/video/BV11Aga6oENR?p=99")
    assert "P3" in link.title
    assert "|" not in (await service.parse_text("BV1xx411c7mD")).title
    await service.aclose()


async def test_bilibili_watchlater_bvid_param():
    service = make_service()
    link = await service.parse_text(
        "https://www.bilibili.com/list/watchlater?bvid=BV1xx411c7mD&oid=123"
    )
    assert link.platform == "B站" and "孤独摇滚" in link.title
    await service.aclose()


async def test_bilibili_dynamic_with_images():
    service = make_service()
    link = await service.parse_text("https://t.bilibili.com/1225943683926327330")
    assert link.platform == "B站动态" and link.author == "七海Nana7mi"
    assert link.title == "生日快乐[星星眼]"
    assert "点赞 1728" in link.stats and "转发 4161" in link.stats
    # 有图集时不再单发封面，避免和图集重复
    assert link.cover is None
    # 正文改走有序内容块：一段文字 + 两张图
    assert link.content == [
        ("text", "生日快乐[星星眼]"),
        ("image", "https://i0.hdslb.com/dyn/a.png"),
        ("image", "https://i0.hdslb.com/dyn/b.png"),
    ]
    # 图文的规范链接取接口给的 opus 跳转地址
    assert link.url == "https://www.bilibili.com/opus/1225943683926327330"
    # /opus/ 与 /dynamic/ 走同一条解析
    same = await service.parse_text("https://www.bilibili.com/opus/1225943683926327330")
    assert same.url == link.url
    await service.aclose()


async def test_bilibili_rich_content_sends_as_forward():
    """图文正文走「摘要卡 + 合并转发」：标题/作者/数据直接发，正文逐段进转发。"""
    service = make_service()
    channel = MediaChannel()
    await service.handle_message(
        group_msg("https://t.bilibili.com/1225943683926327330"), channel
    )
    # 摘要文本直接发，且不含被截断的正文预览
    assert len(channel.sent) == 1
    header = channel.sent[0][1]
    assert "【B站动态】生日快乐[星星眼]" in header
    assert "UP主：七海Nana7mi" in header and "点赞 1728" in header
    # 正文一段文字 + 两张图，作为合并转发的三个节点，图片下载成 base64
    assert len(channel.forwards) == 1
    nodes = channel.forwards[0]
    assert nodes[0] == ("七海Nana7mi", "生日快乐[星星眼]", None)
    assert all(t == "" for _, t, img in nodes if img)  # 图片节点无文字
    # 图片以原始 URL 交给协议端抓取（NapCat 合并转发里 base64 会渲染失败）
    imgs = [img for _, _, img in nodes if img]
    assert imgs == [
        "https://i0.hdslb.com/dyn/a.png",
        "https://i0.hdslb.com/dyn/b.png",
    ]
    # 图片不再单独直发（都在转发里）
    assert channel.images == []
    await service.aclose()


async def test_bilibili_long_opus_fetches_full_content():
    """summary 截断（has_more）的长图文：走 opus/detail 补全整篇正文。"""
    service = make_service()
    link = await service.parse_text(
        "https://www.bilibili.com/opus/1300000000000000001"
    )
    assert link.platform == "B站图文" and link.title == "长篇图文"
    # 完整正文：文字/图片按段交替，代码块并入文字，分割线跳过
    assert link.content == [
        ("text", "第一段正文带链接"),
        ("image", "https://i0.hdslb.com/full-1.png"),
        ("image", "https://i0.hdslb.com/full-2.png"),
        ("text", "第二段正文\nprint(1)"),
        ("image", "https://i0.hdslb.com/full-3.png"),
    ]
    # 不再是被截断的 summary 预览
    assert not any("预览" in v for k, v in link.content if k == "text")
    await service.aclose()


async def test_bilibili_interleaved_long_opus():
    """长图文：富文本内嵌图片就地切断段落，还原「文字→图→文字」的交替顺序。"""
    from botcchi.linkparse.bilibili.dynamic import _from_item

    item = {
        "id_str": "1",
        "type": "DYNAMIC_TYPE_OPUS",
        "basic": {"jump_url": "//www.bilibili.com/opus/1"},
        "modules": {
            "module_author": {"name": "作者"},
            "module_stat": {"like": {"count": 5}},
            "module_dynamic": {
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {
                        "title": "标题",
                        "summary": {
                            "rich_text_nodes": [
                                {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": "第一段"},
                                {
                                    "type": "RICH_TEXT_NODE_TYPE_VIEW_PICTURE",
                                    "pics": [{"src": "https://i0.hdslb.com/p1.png"}],
                                },
                                {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": "第二段"},
                                {
                                    "type": "RICH_TEXT_NODE_TYPE_VIEW_PICTURE",
                                    "pics": [{"src": "https://i0.hdslb.com/p2.png"}],
                                },
                            ]
                        },
                    },
                }
            },
        },
    }
    link = await _from_item(None, item)  # OPUS 不转交，用不到 client
    assert link.platform == "B站图文" and link.title == "标题"
    assert link.content == [
        ("text", "第一段"),
        ("image", "https://i0.hdslb.com/p1.png"),
        ("text", "第二段"),
        ("image", "https://i0.hdslb.com/p2.png"),
    ]


async def test_bilibili_dynamic_forward_and_delegation():
    service = make_service()
    forward = await service.parse_text("https://t.bilibili.com/1225245957853020192")
    assert forward.platform == "B站动态" and forward.title == "恭喜中奖"
    assert "转发 @小问号：视频《驴马杀》" in forward.extra

    # 视频动态转交视频解析器，拿到完整的视频卡与视频流
    video = await service.parse_text("https://t.bilibili.com/1228106449050140729")
    assert video.platform == "B站" and "孤独摇滚" in video.title
    assert video.media.kind == "video"

    # 直播动态转交直播解析器
    live = await service.parse_text("https://t.bilibili.com/1228642821717622800")
    assert live.platform == "B站直播" and live.url == "https://live.bilibili.com/21452505"
    await service.aclose()


async def test_bilibili_live_room():
    service = make_service()
    link = await service.parse_text("https://live.bilibili.com/21452505")
    assert link.platform == "B站直播" and link.title == "二次元的偷"
    assert link.author == "七海Nana7mi" and link.author_label == "主播"
    assert "直播中" in link.stats and "分区 单机游戏 · 主机游戏" in link.stats
    assert "人气 27.3万" in link.stats and "粉丝 111.1万" in link.stats
    # 开播中用关键帧，比静态封面更能反映当下
    assert link.cover == "https://i0.hdslb.com/live/keyframe.jpg"
    assert "七海,海子姐" in link.extra
    await service.aclose()


async def test_bilibili_article_and_favlist():
    service = make_service()
    article = await service.parse_text("https://www.bilibili.com/read/cv1")
    assert article.platform == "B站专栏" and article.title == "未知的光"
    assert article.author == "健行见远渐忘" and "阅读 166.9万" in article.stats
    assert article.cover == "https://i0.hdslb.com/article/banner.jpg"

    fav = await service.parse_text(
        "https://space.bilibili.com/1/favlist?fid=3117418552&ftype=create"
    )
    assert fav.platform == "B站收藏夹" and fav.title == "吉他教学"
    assert fav.author_label == "创建者" and "内容 3 个" in fav.stats
    assert fav.extra == "第一课；第二课；第三课"
    await service.aclose()


async def test_bilibili_risk_control_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -352, "message": "-352"})

    service = LinkParseService(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ParseError, match="风控"):
        await service.parse_text("https://www.bilibili.com/read/cv1")
    await service.aclose()


async def test_bilibili_video_refreshes_buvid_after_http_412():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "frontend/finger/spi" in url:
            calls.append("spi")
            suffix = calls.count("spi")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"b_3": f"buvid3-{suffix}", "b_4": f"buvid4-{suffix}"},
                },
            )
        if "x/web-interface/view" in url:
            calls.append("view")
            if calls.count("view") == 1:
                return httpx.Response(412)
            assert "buvid3-2" in request.headers.get("Cookie", "")
            return httpx.Response(200, json=BILI_VIEW)
        if "x/player/playurl" in url:
            return httpx.Response(200, json={"code": -404})
        raise AssertionError(url)

    service = LinkParseService(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    link = await service.parse_text("BV1xx411c7mD")
    assert link is not None and link.title == BILI_VIEW["data"]["title"]
    assert calls == ["spi", "view", "spi", "view"]
    await service.aclose()


async def test_bilibili_short_link_redirect():
    service = make_service()
    link = await service.parse_text("https://b23.tv/abc123 分享给你")
    assert link is not None and "孤独摇滚" in link.title
    await service.aclose()


async def test_douyin_short_link():
    service = make_service()
    link = await service.parse_text(
        "8.88 复制打开抖音 https://v.douyin.com/_2ljF4AmKL8/ 看看"
    )
    assert link.platform == "抖音视频"
    assert link.title == "吉他英雄的日常练习" and link.author == "后藤一里"
    assert "点赞 1.5万" in link.stats and link.duration == "0:33"
    assert link.cover_referer == "https://www.douyin.com/"
    assert link.media.url == "https://v.example/1.mp4"  # playwm 去水印后
    await service.aclose()


async def test_douyin_long_link_forms():
    service = make_service()
    # PC 长链、iesdouyin share、m.douyin share 都归一到分享页
    for url in (
        "https://www.douyin.com/video/7521023890996514083",
        "https://www.iesdouyin.com/share/video/7521023890996514083/?region=CN",
        "https://m.douyin.com/share/video/7521023890996514083",
    ):
        link = await service.parse_text(url)
        assert link.platform == "抖音视频" and link.title == "吉他英雄的日常练习"
    await service.aclose()


async def test_douyin_note_livephoto():
    service = make_service()
    # jx 短链跳到图文；Live Photo 只取静图，两张图都收
    link = await service.parse_text("https://jx.douyin.com/AbCdEf/")
    assert link.platform == "抖音图集（2图）" and link.author == "喜多郁代"
    assert link.title == "秋日碎片"
    assert link.media.kind == "images"
    assert link.media.urls == [
        "https://p.example/img1.jpg",
        "https://p.example/img2.jpg",
    ]
    # 图集整组转发，卡片不再单发封面
    assert link.cover is None
    assert link.url == "https://www.douyin.com/note/7469411074119322899"
    await service.aclose()


async def test_douyin_slides():
    service = make_service()
    link = await service.parse_text(
        "https://www.douyin.com/slides/7469411074119322899"
    )
    assert link.platform == "抖音图集（2图）" and link.media.kind == "images"
    await service.aclose()


async def test_parse_error_on_deleted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": -404, "message": "啥都木有"}
        )

    service = LinkParseService(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ParseError, match="-404"):
        await service.parse_text("https://www.bilibili.com/video/BV1xx411c7mD")
    await service.aclose()


async def test_handle_message_sends_card_and_cooldown():
    service = make_service()
    channel = ImageChannel()

    handled = await service.handle_message(
        group_msg("https://b23.tv/abc123"), channel
    )
    assert handled
    assert len(channel.images) == 1 and channel.images[0].startswith("base64://")
    text = channel.sent[-1][1]
    assert text.startswith("【B站】") and "UP主：小孤独" in text
    assert "https://www.bilibili.com/video/BV1xx411c7mD" in text

    # 冷却期内同链接不再解析（不消费，交回正常消息流）
    assert not await service.handle_message(group_msg("https://b23.tv/abc123"), channel)
    # 其他群不受影响
    assert await service.handle_message(
        group_msg("https://b23.tv/abc123", chat_id="888"), channel
    )
    assert COOLDOWN_SECONDS > 0
    await service.aclose()


async def test_weibo_image_status():
    service = make_service()
    link = await service.parse_text("https://m.weibo.cn/status/5181502771168068")
    assert link.platform == "微博（2图）" and link.author == "微博用户"
    assert "今天天气真不错" in link.title
    assert "http://t.cn" not in link.title  # 长文尾链已剥离
    assert "点赞 100" in link.stats and "转发 10" in link.stats
    # livephoto 也当普通图片取，取 large 直链
    assert link.media.kind == "images" and len(link.media.urls) == 2
    assert all("sinaimg.cn" in u for u in link.media.urls)
    assert link.url == "https://weibo.com/7207262816/Pycg54TRO"
    await service.aclose()


async def test_weibo_longtext_video_repost():
    service = make_service()
    link = await service.parse_text("https://weibo.com/7207262816/P5kWdcfDe")
    # isLongText 走 longtext 接口取完整正文
    assert "完整长文正文" in link.title
    # page_info 视频转成可转发 video，时长来自 media_info
    assert link.media.kind == "video" and link.media.duration == 14
    assert link.media.url == "https://f.video.weibocdn.com/hd.mp4"
    # 视频场景用 page_pic 作封面
    assert link.cover == "https://wx.sinaimg.cn/video-cover.jpg"
    # 转发附上被转微博的一句话摘要
    assert "转发 @原博主：被转发的原文内容" in link.extra
    await service.aclose()


async def test_weibo_article():
    service = make_service()
    link = await service.parse_text(
        "https://card.weibo.com/article/m/show/id/2309404962180771742222"
    )
    assert link.platform == "微博文章" and link.title == "小米14的存储扩容"
    assert link.author == "小米澎湃OS" and "阅读 19万+" in link.stats
    assert link.cover == "https://wx.sinaimg.cn/large/article-cover.jpg"
    # ttarticle 查询串形式同样命中
    tt = await service.parse_text(
        "https://weibo.com/ttarticle/p/show?id=2309404962180771742222"
    )
    assert tt.platform == "微博文章"
    await service.aclose()


async def test_weibo_visitor_handshake_cached():
    service = make_service()
    await service.parse_text("https://m.weibo.cn/status/5181502771168068")
    await service.parse_text("https://m.weibo.cn/status/5181502771168068")
    # 访客握手只做一次，后续复用缓存
    genvisitor = [r for r in service.requests if "genvisitor2" in str(r.url)]
    assert len(genvisitor) == 1
    await service.aclose()


async def test_rednote_parser():
    service = make_service()
    link = await service.parse_text("http://xhslink.com/o/AbCdEf 快看")
    assert link.platform.startswith("小红书图文") and link.author == "小红薯"
    assert link.title == "秋日穿搭分享"
    assert "点赞 1.2万" in link.stats
    assert link.media.kind == "images" and len(link.media.urls) == 3
    assert all("ci.xiaohongshu.com" in u for u in link.media.urls)
    await service.aclose()


async def test_rednote_expired_token():
    service = make_service()
    # token 失效被重定向到 /404/ 安全页，给出明确提示而非"数据为空"
    with pytest.raises(ParseError, match="xsec_token"):
        await service.parse_text(
            "https://www.xiaohongshu.com/explore/abc123?xsec_token=EXPIRED"
        )
    await service.aclose()


def test_rednote_video_url_fallback():
    from botcchi.linkparse.rednote import _video_url

    # 优先 originVideoKey 拼 CDN
    assert _video_url(
        {"consumer": {"originVideoKey": "abc/def"}}
    ) == "https://sns-video-bd.xhscdn.com/abc/def"
    # 无 key 时退回流地址 masterUrl（按编码优先级）
    assert (
        _video_url(
            {
                "consumer": {"originVideoKey": ""},
                "media": {"stream": {"h264": [{"masterUrl": "https://x/hd.mp4"}]}},
            }
        )
        == "https://x/hd.mp4"
    )
    # 都拿不到返回 None，退回摘要卡
    assert _video_url({"consumer": {}}) is None


async def test_kuaishou_parser():
    service = make_service()
    link = await service.parse_text("https://v.kuaishou.com/2yAnzeZ")
    assert link.platform == "快手视频" and link.author == "快手用户"
    assert link.title == "快手上的猫猫" and link.duration == "0:15"
    assert "播放 5万" in link.stats
    assert link.media.kind == "video"
    assert link.media.url == "https://ks.example/video.mp4"
    await service.aclose()


async def test_kuaishou_direct_and_www_links():
    service = make_service()
    # 字母数字 photoId 的直链（旧版 \d+ 正则会漏解析）
    direct = await service.parse_text(
        "https://v.m.chenzhongtech.com/fw/photo/3xkuaishou123"
    )
    assert direct.platform == "快手视频" and direct.title == "快手上的猫猫"
    # www.kuaishou.com 分享短链
    www = await service.parse_text("https://www.kuaishou.com/f/XxYyZz")
    assert www.platform == "快手视频" and www.author == "快手用户"
    await service.aclose()


async def test_x_video_and_quoted():
    service = make_service()
    link = await service.parse_text(
        "https://x.com/vtuber_x/status/1900000000000000001"
    )
    assert link.platform == "X（视频）"
    assert link.title == "看这个视频！"  # 尾部 t.co 媒体链接被剥掉
    assert link.author == "主播娘（@vtuber_x）"
    assert link.duration == "0:06"
    assert "浏览 17万" in link.stats and "转发 1426" in link.stats
    # 挑最高码率 mp4
    assert link.media.kind == "video" and link.media.url == "https://v/hi.mp4"
    assert link.cover.startswith("https://pbs.twimg.com/amplify_video_thumb")
    # 引用推文一句话摘要
    assert link.extra == "引用 被引用者（@orig_user）：这是被引用的原推文内容"
    assert link.url == "https://x.com/vtuber_x/status/1900000000000000001"
    await service.aclose()


async def test_x_old_tweet_without_view_count():
    """老推文 views 无 count，stats 不应崩溃且省略浏览数（参照模型此处会挂）。"""
    service = make_service()
    link = await service.parse_text("https://twitter.com/jack/status/20")
    assert link.platform == "X"  # 无媒体
    assert link.title == "just setting up my twttr"
    assert "浏览" not in link.stats
    assert link.stats.startswith("点赞 30.7万")
    assert link.media is None and link.cover is None
    await service.aclose()


async def test_x_deleted_tweet():
    service = make_service()
    with pytest.raises(ParseError, match="不存在"):
        await service.parse_text("https://x.com/i/status/1900000000000000404")
    await service.aclose()


async def test_zhihu_article():
    service = make_service()
    link = await service.parse_text("https://zhuanlan.zhihu.com/p/584852445")
    assert link.platform == "知乎文章"
    assert link.title == "打开链接发现要提取码怎么办" and link.author == "我呀"
    assert "赞同 62" in link.stats and "评论 41" in link.stats and "IP 湖南" in link.stats
    assert link.extra == "正文节选内容在这里"  # excerpt 里的 img 标签被剥掉
    assert link.cover == "https://pic1.zhimg.com/v2-title.jpg"  # title_image 优先
    assert link.url == "https://zhuanlan.zhihu.com/p/584852445"
    await service.aclose()


async def test_zhihu_question():
    service = make_service()
    link = await service.parse_text("https://www.zhihu.com/question/19550225")
    assert link.platform == "知乎问题"
    assert link.author_label == "提问" and link.author == "胡维"
    assert "1489 回答" in link.stats and "浏览 281.5万" in link.stats
    assert link.extra == "这是问题的详细描述正文。"
    assert link.url == "https://www.zhihu.com/question/19550225"
    await service.aclose()


async def test_zhihu_answer_and_short_link():
    service = make_service()
    # 完整问答链接
    full = await service.parse_text(
        "https://www.zhihu.com/question/67423622/answer/315796660"
    )
    assert full.platform == "知乎回答" and full.author_label == "答主"
    assert full.title == "怎么女装过安检？" and full.author == "小六花"
    assert "赞同 2903" in full.stats and "感谢 456" in full.stats
    assert full.cover == "https://pica.zhimg.com/v2-ans.jpg"
    # 短链形（无 qid），规范化 url 由响应里的 question.id 补齐
    short = await service.parse_text("https://www.zhihu.com/answer/315796660")
    assert short.url == "https://www.zhihu.com/question/67423622/answer/315796660"
    await service.aclose()


async def test_tieba_text_post():
    service = make_service()
    link = await service.parse_text("https://tieba.baidu.com/p/9451359896")
    assert link.platform == "贴吧·genshin吧"
    assert link.title == "原神藤编八奇活动" and link.author == "琴非瑟"
    assert link.author_label == "楼主"
    assert link.stats == "回复 1"
    assert "活动正文" in link.extra and "@某人" in link.extra
    assert link.url == "https://tieba.baidu.com/p/9451359896"
    assert link.media is None
    await service.aclose()


async def test_tieba_image_post():
    service = make_service()
    link = await service.parse_text("https://tieba.baidu.com/p/10855044566")
    assert link.platform == "贴吧·史尔特尔吧"
    assert "点赞 3" in link.stats and "回复 3" in link.stats
    assert link.media.kind == "images" and len(link.media.urls) == 2
    assert link.cover is None  # 图集转发时封面重复，交给 send_card 去重
    assert link.media.headers.get("Referer") == "https://tieba.baidu.com/"
    await service.aclose()


async def test_tieba_video_post():
    service = make_service()
    link = await service.parse_text("https://tieba.baidu.com/p/8000000001")
    assert "浏览 8.8万" in link.stats
    assert link.media.kind == "video" and link.media.url == "https://vid.tb/v.mp4"
    assert link.media.duration == 30
    assert link.cover == "https://vid.tb/cover.jpg"
    assert link.cover_referer == "https://tieba.baidu.com/"
    await service.aclose()


async def test_tieba_deleted_post():
    service = make_service()
    with pytest.raises(ParseError, match="删除"):
        await service.parse_text("https://tieba.baidu.com/p/9101148197")
    await service.aclose()


async def test_netease_parser_and_audio_send():
    service = make_service()
    link = await service.parse_text("https://music.163.com/song/22677536")
    assert link.platform == "网易云音乐"
    assert link.title == "青春コンプレックス" and link.author == "結束バンド"
    assert link.duration == "3:45"
    assert link.media.kind == "audio" and link.media.duration == 225

    channel = MediaChannel()
    await service.send_card(group_msg("").session, channel, link)
    assert len(channel.records) == 1  # 歌曲音频已发
    assert channel.records[0].startswith("base64://")
    await service.aclose()


async def test_bilibili_video_media_send():
    service = make_service(max_video_seconds=800)  # 夹具视频 754s
    channel = MediaChannel()
    await service.handle_message(
        group_msg("https://www.bilibili.com/video/BV1xx411c7mD"), channel
    )
    # 封面 + 文本 + 视频
    assert len(channel.images) == 1
    assert len(channel.videos) == 1 and channel.videos[0].startswith("base64://")
    assert "超出" not in channel.sent[-1][1]
    await service.aclose()


async def test_media_limits():
    # 时长超限：不下载视频，卡片附提示
    service = make_service(max_video_seconds=60)
    channel = MediaChannel()
    await service.handle_message(
        group_msg("https://www.bilibili.com/video/BV1xx411c7mD"), channel
    )
    assert channel.videos == []
    assert "超出转发时长限制" in channel.sent[-1][1]
    await service.aclose()

    # 大小超限（durl 自带 size=5MB > 1MB 上限；时长上限放宽以便命中大小检查）
    service = make_service(max_media_mb=1, max_video_seconds=800)
    channel = MediaChannel()
    await service.handle_message(
        group_msg("https://www.bilibili.com/video/BV1xx411c7mD"), channel
    )
    assert channel.videos == []
    assert "太大" in channel.sent[-1][1]
    await service.aclose()

    # send_media 关闭：只发摘要卡且无提示
    service = make_service(send_media=False)
    channel = MediaChannel()
    await service.handle_message(
        group_msg("https://www.bilibili.com/video/BV1xx411c7mD"), channel
    )
    assert channel.videos == [] and "超出" not in channel.sent[-1][1]
    await service.aclose()


async def test_images_capped_and_cover_skipped():
    service = make_service(max_images=2)
    channel = MediaChannel()
    await service.handle_message(
        group_msg("https://m.weibo.cn/status/5181502771168068"), channel
    )
    # 图集场景跳过封面，只发前 max_images 张
    assert len(channel.images) == 2
    assert channel.sent[-1][1].startswith("【微博（2图）】")
    await service.aclose()


async def test_download_capped_aborts_midstream():
    from botcchi.linkparse.media import download_capped

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1024)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await download_capped(client, "https://a/b", {}, 512) is None
    assert await download_capped(client, "https://a/b", {}, 2048) == b"x" * 1024
    await client.aclose()


async def test_acfun_parser():
    ks_play = json.dumps(
        {
            "adaptationSet": [
                {
                    "representation": [
                        {"url": "https://ali.acfun.example/high.m3u8"},
                        {"url": "https://ali.acfun.example/low.m3u8"},
                    ]
                }
            ]
        }
    )
    info = {
        "title": "AC娘的日常",
        "user": {"name": "阿婆主"},
        "durationMillis": 90000,
        "viewCount": 8000,
        "danmakuCount": 300,
        "description": "测试视频",
        "coverUrl": "https://acfun.example/cover.jpg",
        "currentVideoInfo": {"ksPlayJson": ks_play},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "acfun.cn/v/ac" in str(request.url):
            html = (
                "<html><script>window.videoInfo ="
                + json.dumps(info, ensure_ascii=False)
                + "</script></html>"
            )
            return httpx.Response(200, text=html)
        return httpx.Response(404)

    from botcchi.config import LinkParseSettings

    service = LinkParseService(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        settings=LinkParseSettings(),
    )
    link = await service.parse_text("https://www.acfun.cn/v/ac12345678")
    assert link.platform == "AcFun" and link.author == "阿婆主"
    assert link.duration == "1:30"
    # 取最低清晰度流
    assert link.media.kind == "m3u8" and "low.m3u8" in link.media.url
    await service.aclose()


async def test_service_integration_and_feature_gate(mock_openai, tmp_path):
    """群普通消息触发被动解析；关闭「解析」功能后不再触发。"""
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    settings.onebot.superusers = ["900000"]
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    # 注入 mock 网络
    await ctx.service.linkparse.client.aclose()
    ctx.service.linkparse.client = make_service().client

    channel = ImageChannel()
    await ctx.service.handle(
        group_msg("看看这个 https://www.bilibili.com/video/BV1xx411c7mD"), channel
    )
    assert channel.sent and channel.sent[-1][1].startswith("【B站】")
    assert mock_openai.requests == []  # 未惊动 LLM

    # 超管关闭解析功能后，同类消息不再响应
    block_cmd = group_msg("关闭功能 解析", chat_id="999")
    block_cmd.user_id = "900000"
    await ctx.service.handle(block_cmd, channel)
    before = len(channel.sent)
    await ctx.service.handle(
        group_msg(
            "https://www.bilibili.com/video/BV1xx411c7mD", chat_id="999"
        ),
        channel,
    )
    assert len(channel.sent) == before
    await ctx.shutdown()


async def test_parse_link_tool(mock_openai, tmp_path):
    from botcchi.app import build_context
    from botcchi.tools.registry import ToolContext

    settings = make_settings(tmp_path)
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.service.linkparse.client.aclose()
    ctx.service.linkparse.client = make_service().client

    tool = ctx.service.loop.tools.get("parse_link")
    channel = ImageChannel()
    tool_ctx = ToolContext(
        incoming=group_msg("", at_me=True), channel=channel, bot_name="波奇"
    )
    out = await tool.execute('{"url": "https://b23.tv/abc123"}', tool_ctx)
    assert "已发送" in out and "孤独摇滚" in out
    assert channel.sent and channel.images

    out = await tool.execute('{"url": "https://example.com/x"}', tool_ctx)
    assert "没有识别出" in out
    await ctx.shutdown()


def test_json_card_url_extraction():
    from botcchi.channels.onebot.adapter import OneBotAdapter, _extract_card_url
    from botcchi.config import OneBotSettings

    raw = json.dumps(
        {
            "app": "com.tencent.miniapp_01",
            "meta": {
                "detail_1": {
                    "title": "哔哩哔哩",
                    "qqdocurl": "https://b23.tv/abc123?share_medium=android",
                }
            },
        }
    )
    assert _extract_card_url(raw).startswith("https://b23.tv/abc123")
    assert _extract_card_url("not json") is None
    assert _extract_card_url(json.dumps({"meta": {}})) is None

    adapter = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 777,
        "user_id": 10001,
        "message_id": 1,
        "sender": {"nickname": "hhk"},
        "message": [{"type": "json", "data": {"data": raw}}],
    }
    incoming = adapter._parse_message_event(event, self_id="999")
    assert "https://b23.tv/abc123" in incoming.text


async def test_schedule_tools_with_permission(mock_openai, tmp_path):
    """自然语言定时：schedule_daily_report / schedule_group_summary 工具。"""
    from botcchi.app import build_context
    from botcchi.report.service import KIND_DAILY_REPORT
    from botcchi.summary import KIND_GROUP_SUMMARY
    from botcchi.tools.registry import ToolContext

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    channel = FakeChannel()

    def tool_ctx(role=None) -> ToolContext:
        incoming = group_msg("", at_me=True)
        incoming.sender_role = role
        return ToolContext(incoming=incoming, channel=channel, bot_name="波奇")

    report_tool = ctx.service.loop.tools.get("schedule_daily_report")
    summary_tool = ctx.service.loop.tools.get("schedule_group_summary")

    # 无权限用户被拒，任务未创建
    out = await report_tool.execute('{"hour": 8}', tool_ctx())
    assert "管理权限" in out
    assert await ctx.jobs.list_jobs(kind=KIND_DAILY_REPORT) == []

    # 群管理员设置成功
    out = await report_tool.execute('{"hour": 8}', tool_ctx(role="admin"))
    assert "每天 8 点" in out
    (job,) = await ctx.jobs.list_jobs(kind=KIND_DAILY_REPORT)
    assert job.recurrence == "daily@08:00" and job.session.chat_id == "777"

    out = await summary_tool.execute(
        '{"hour": 22, "min_messages": 30}', tool_ctx(role="admin")
    )
    assert "每天 22 点" in out and "30 条" in out
    (job,) = await ctx.jobs.list_jobs(kind=KIND_GROUP_SUMMARY)
    assert job.payload == {"min_messages": 30}

    # 取消
    out = await report_tool.execute('{"cancel": true}', tool_ctx(role="admin"))
    assert "已取消" in out
    assert await ctx.jobs.list_jobs(kind=KIND_DAILY_REPORT) == []

    # 私聊场景拒绝
    private = IncomingMessage(
        session=SessionKey(channel="fake", chat_type="private", chat_id="1"),
        user_id="1", user_name="u", text="", at_me=True,
    )
    out = await report_tool.execute(
        '{"hour": 8}',
        ToolContext(incoming=private, channel=channel, bot_name="波奇"),
    )
    assert "群聊" in out
    await ctx.shutdown()
