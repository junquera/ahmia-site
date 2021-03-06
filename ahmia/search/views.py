"""
Views
Full text search views.
"""
import logging
import math
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from django.template import loader

from ahmia import utils
from ahmia.lib.pagepop import PagePopHandler
from ahmia.models import SearchResultsClick, SearchQuery, PagePopScore
from ahmia.utils import get_elasticsearch_i2p_index
from ahmia.validators import is_valid_full_onion_url
from ahmia.views import ElasticsearchBaseListView

logger = logging.getLogger("search")


def onion_redirect(request):
    """Add clicked information and redirect to .onion address."""

    redirect_url = request.GET.get('redirect_url', '')
    search_term = request.GET.get('search_term', '')

    if not redirect_url or not search_term:
        answer = "Bad request: no GET parameter URL."
        return HttpResponseBadRequest(answer)

    try:
        onion = utils.extract_domain_from_url(redirect_url)
        if is_valid_full_onion_url(redirect_url):
            # currently we can't log i2p clicks due to
            # SearchResultsClick.onion_domain having an onion validator
            # Also we don't have yet i2p results in order to test it
            SearchResultsClick.objects.add_or_increment(
                onion_domain=onion,
                clicked=redirect_url,
                search_term=search_term)
    except Exception as error:
        logger.error("Error with redirect URL: {0}\n{1}".format(
            redirect_url, error))

    message = "Redirecting to hidden service."
    return redirect_page(message, 0, redirect_url)


def redirect_page(message, red_time, url):
    """Build and return redirect page."""

    template = loader.get_template('redirect.html')
    content = {'message': message, 'time': red_time, 'redirect': url}
    return HttpResponse(template.render(content))


def filter_hits_by_time(hits, pastdays):
    """Return only the hits that were crawled the past pastdays"""

    time_threshold = datetime.fromtimestamp(
        time.time()) - timedelta(days=pastdays)
    ret = [hit for hit in hits if hit['updated_on'] >= time_threshold]
    return ret


def heuristic_score(ir_score, gp_score, lp_score, urlparams):
    """
    A formula to combine IR score given by Elasticsearch and
    PagePop scores given by page popularity algorithm. Arithmetics is
    black art, it can only improve via manually testing queries, and
    user feedback. Currently its a simple weighted sum.

    Normally the more tokens in query the lower pagepop influence
    should be. However the more tokens given the more ES seems to
    diverge IR score. Thus we bypass 'number of tokens' for the moment

    todo: hardcode coefficient values (currently in url params) and
    todo: make local pagepop coeff: `lp_coeff` proportional to number of hits

    :param ir_score: Information Relevance score by Elasticsearch
    :param gp_score: Global popularity score for that domain
    :param lp_score: Local popularity score for that domain
    :return: final score
    :rtype: ``float``
    """
    lp_coeff = float(urlparams.get('lp', 0))
    gp_coeff = float(urlparams.get('gp', 0))
    ir_coeff = 1 - gp_coeff - lp_coeff
    ret = gp_score * gp_coeff + lp_score * lp_coeff + ir_score * ir_coeff

    # drag down the average score when the two scores diverge too much
    # pp = gp_coeff * gp_score
    # ir = ir_coeff * ir_score
    # ret = pp * ir / (pp + ir)  # failed: too much of a penalty

    return ret


def local_page_pop(hits):
    """Calculate page popularity only for the domains of our results (hits)"""
    domains = set(h['domain'] for h in hits)

    p = PagePopHandler(hits, domains)
    p.build_pagescores()
    scores = p.get_scores_as_dict()

    return scores


class TorResultsView(ElasticsearchBaseListView):
    """ Search results view """

    http_method_names = ['get']
    template_name = "tor_results.html"
    RESULTS_PER_PAGE = 100

    def get(self, request, *args, **kwargs):
        """
        This method is override to add parameters to the get_context_data call
        """
        start = time.time()
        kwargs['q'] = request.GET.get('q', '')
        kwargs['page'] = request.GET.get('page', 0)

        self.log_stats(**kwargs)

        self.get_queryset(**kwargs)

        if 'gp' in request.GET or 'lp' in request.GET:  # enable PagePop
            local_pp_scores = local_page_pop(self.object_list.hits)
            self.sort_hits(local_pp_scores, request.GET)

        self.filter_hits()

        kwargs['time'] = round(time.time() - start, 2)

        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    @staticmethod
    def log_stats(**kwargs):
        """log the query for stats calculations"""
        SearchQuery.objects.add_or_increment(
            search_term=kwargs['q'], network='T')

    # def get_queryset(self, **kwargs):
    #     object_list = super(TorResultsView, self).get_queryset(**kwargs)
    #     object_list.hits = self.filter_hits(object_list.hits)
    #     return object_list

    def get_es_context(self, **kwargs):
        return {
            "index": utils.get_elasticsearch_tor_index(),
            "doc_type": utils.get_elasticsearch_type(),
            "body": {
                "query": {
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": kwargs['q'],
                                    "type": "most_fields",
                                    "fields": [
                                        'title^6',
                                        'anchor^6',
                                        'fancy.shingles^3',
                                        'fancy.stemmed^3',
                                        'fancy^3',
                                        'content^1',
                                    ],
                                    "minimum_should_match": "75%",
                                    "cutoff_frequency": 0.01
                                }
                            }
                        ],
                        "must_not": [
                            {
                                "exists": {
                                    # todo duplicate key since its defined as python dict
                                    "field": "is_fake",
                                    "field": "is_banned"
                                }
                            }
                        ]
                        # "filter": [
                        #     {
                        #         "missing": {
                        #             "field": "is_fake"
                        #         }
                        #     },
                        #     {
                        #         "missing": {
                        #             "field": "is_banned"
                        #         }
                        #     }
                        # ]
                    }
                },
                "suggest": {
                    "text": kwargs.get('q'),
                    "simple-phrase": {
                        "phrase": {
                            "field": "fancy",
                            "gram_size": 2  # todo make this applicable?
                        }
                    }
                },
                "aggregations": {
                    "domains": {
                        "terms": {
                            "size": 1000,
                            "field": "domain",
                            "order": {"max_score": "desc"}
                        },
                        "aggregations": {
                            "score": {
                                "top_hits": {
                                    "size": 1,
                                    "sort": [
                                        {
                                            "authority": {
                                                "order": "desc",
                                                "missing": 0.0000000001
                                            }
                                        },
                                        {
                                            "_score": {
                                                "order": "desc"
                                            }
                                        }
                                    ],
                                    "_source": {
                                        "include": ["title", "url", "meta",
                                                    "updated_on", "domain",
                                                    "authority", "anchors",
                                                    "links"]
                                    }
                                }
                            },
                            "max_score": {
                                "max": {
                                    "script": "_score"
                                }
                            },
                        }
                    }
                }
            },
            "size": 0
        }

    def format_hits(self, hits):
        """
        Transform ES response into a list of results.
        Returns total number of results, results, didYoMean suggestion

        :param hits: ES response, type: `dict`
        :rtype: SimpleNamespace
        """
        try:
            suggest = hits['suggest']['simple-phrase'][0]['options'][0]['text']
        except (KeyError, IndexError, TypeError):
            suggest = None
        hits = hits['aggregations']['domains']
        total = len(hits['buckets']) + hits['sum_other_doc_count']

        new_hits = []
        for h in hits['buckets']:
            # replace score, updated_on, anchors with clear values
            tmp = h['score']['hits']['hits'][0]
            new_hit = tmp['_source'].copy()
            new_hit['score'] = tmp['sort'][1] * tmp['sort'][0]
            new_hit['updated_on'] = datetime.strptime(
                new_hit['updated_on'], '%Y-%m-%dT%H:%M:%S')
            try:
                new_hit['anchors'] = new_hit['anchors'][0]
            except (KeyError, TypeError):
                pass

            new_hits.append(new_hit)

        self.object_list = SimpleNamespace(total=total, hits=new_hits, suggest=suggest)

    def filter_hits(self):
        url_params = self.request.GET
        hits = self.object_list.hits
        try:
            pastdays = int(url_params.get('d'))
        except (TypeError, ValueError):
            # Either pastdays not exists or not valid int (e.g 'all')
            # Either case hits are not altered
            pass
        else:
            hits = filter_hits_by_time(hits, pastdays)
            self.object_list.hits = hits
            self.object_list.total = len(hits)

    def sort_hits(self, local_pp_scores, urlparams):
        """
        Combine IR (Information Relevant) score given by Elasticsearch,
        with PP (Page Popularity) score, to sort the results
        """
        if not self.object_list:
            return
        hits = self.object_list.hits

        ir_scores = []
        pp_globl_scores = []
        pp_local_scores = []
        for h in hits:
            ir_scores.append(h.get('score', 0))
            pp_globl_scores.append(PagePopScore.objects.get_score(onion=h['domain']))
            pp_local_scores.append(local_pp_scores[h['domain']])

        ir_scores_norm = utils.normalize_on_max(ir_scores)
        pp_globl_scores_norm = utils.normalize_on_max(pp_globl_scores)
        pp_local_scores_norm = utils.normalize_on_max(pp_local_scores)

        if settings.DEBUG:
            assert len(ir_scores_norm) == len(pp_globl_scores_norm) == \
                   len(pp_local_scores_norm) == len(hits)

        for h, ir, pp, pl in zip(hits, ir_scores_norm, pp_globl_scores_norm,
                                 pp_local_scores_norm):
            h['score'] = heuristic_score(ir, pp, pl, urlparams)

        self.object_list.hits = sorted(hits, key=lambda k: k['score'], reverse=True)

    def get_context_data(self, **kwargs):
        """
        Get the context data to render the result page.
        """
        page = kwargs['page']
        length = self.object_list.total
        max_pages = int(math.ceil(float(length) / self.RESULTS_PER_PAGE))

        return {
            'suggest': self.object_list.suggest,
            'page': page + 1,
            'max_pages': max_pages,
            'result_begin': self.RESULTS_PER_PAGE * page,
            'result_end': self.RESULTS_PER_PAGE * (page + 1),
            'total_search_results': length,
            'query_string': kwargs['q'],
            'search_results': self.object_list.hits,
            'search_time': kwargs['time'],
            'now': date.fromtimestamp(time.time())
        }


class IipResultsView(TorResultsView):
    """ I2P Search results view """
    template_name = "i2p_results.html"

    @staticmethod
    def log_stats(**kwargs):
        """Invoked by super().get() to log the query for stats calculations"""
        SearchQuery.objects.add_or_increment(
            search_term=kwargs['q'], network='I')

    def get_es_context(self, **kwargs):
        context = super(IipResultsView, self).get_es_context(**kwargs)
        context['index'] = get_elasticsearch_i2p_index()
        return context
