import scrapy

class ScholarshipItem(scrapy.Item):
    name = scrapy.Field()
    provider = scrapy.Field()
    source_type = scrapy.Field()
    official_source_url = scrapy.Field()
    application_url = scrapy.Field()
    amount_benefit = scrapy.Field()
    education_level = scrapy.Field()
    income_criteria = scrapy.Field()
    gender_criteria = scrapy.Field()
    category_criteria = scrapy.Field()
    domicile_criteria = scrapy.Field()
    opening_date = scrapy.Field()
    closing_date = scrapy.Field()
    selection_process = scrapy.Field()
    documents_required = scrapy.Field()
    slug = scrapy.Field()
    
    # We pass the evidence array to the pipeline for scoring
    evidence_list = scrapy.Field()