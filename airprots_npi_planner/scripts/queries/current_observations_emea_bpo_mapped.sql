-- Query:        EMEA-wide current observations, FILTERED via the canonical
--               BPO mapping table (kirby_external_data.emea_npi_product_mapping)
--               with product_number=1 (the primary tracked Uber product per
--               country). Source/country/(npi_city OR city_order=1 fallback)
--               join logic mirrors Anton's reporting query exactly.
--
--               Output: one row per (airport_code, trip_type, hex_id,
--               time_bucket, dist_bucket, competitor) with `num_observations`
--               summed over the lookback window. v5 reads this for its
--               baseline KPI; downstream we further filter to the right
--               competitor per airport using the airport→competitor sheet.
--
-- Tables:       airport.airports, kirby_external_data.emea_npi_product_mapping,
--               emea_ops.npi_individual_trip
--
-- Parameters:
--   {{start_date}}  YYYY-MM-DD inclusive
--   {{end_date}}    YYYY-MM-DD exclusive
--   {{hex_size}}    H3 resolution (int), e.g. 7

with
    geos as (
        select distinct
            a.airport_code                                          as airport_code,
            cast(a.city_id as varchar)                              as airport_city_id,
            a.airport_master_geofence_simplified_shape_wkt          as air_shape_wkt
        from airport.airports as a
        where 1=1
            and a.mega_region = 'EMEA'
            and a.is_operational
            and not a.is_deleted),

    mapping_v2 as (
        select
            country,
            npi_city,
            uber_product,
            comp_products as comp_product,
            product_number,
            city_order,
            source_active_ as source
        from kirby_external_data.emea_npi_product_mapping),

    obs_raw as (
        select
            coalesce(pu_g.airport_code, do_g.airport_code)          as airport_code,
            case
                when pu_g.airport_code is not null then 'PU'
                else 'DO'
            end                                                     as trip_type,

            -- hex_id of the city end of the trip (PU -> dropoff; DO -> pickup)
            case
                when pu_g.airport_code is not null
                    then get_hexagon_addr(cast(np.dropoff_lat as double),
                                          cast(np.dropoff_lng as double),
                                          {{hex_size}})
                else get_hexagon_addr(cast(np.pickup_lat as double),
                                      cast(np.pickup_lng as double),
                                      {{hex_size}})
            end                                                     as hex_id,

            np.city_id                                              as city_id,
            -- IMPORTANT: np.competitor is actually the UBER PRODUCT name
            -- (e.g. 'UberX'). The real competitor identity (Bolt, Cabify…)
            -- is m.comp_product from the mapping. We expose comp_product
            -- here so v5 can pick the right row per the airport→competitor
            -- sheet.
            m.comp_product                                          as competitor,
            cast(date_parse(np.timestamp_local, '%Y-%m-%d %H:%i:%s') as timestamp) as ts_local,
            cast(np.google_distance as double)                      as dist_km
        from emea_ops.npi_individual_trip as np
            -- BPO mapping (canonical reporting): join on uber_product, comp_product,
            -- product_number=1, source, country. City fallback applied below.
            inner join mapping_v2 as m
                on m.uber_product = np.competitor
                    and m.comp_product = np.competitor_app
                    and m.product_number = 1
                    and m.source = np.source
                    and m.country = np.country
            left join geos as pu_g
                on st_contains(pu_g.air_shape_wkt,
                               st_point(cast(np.pickup_lng  as double),
                                        cast(np.pickup_lat  as double)))
                    and pu_g.airport_city_id = np.city_id
            left join geos as do_g
                on st_contains(do_g.air_shape_wkt,
                               st_point(cast(np.dropoff_lng as double),
                                        cast(np.dropoff_lat as double)))
                    and do_g.airport_city_id = np.city_id
        where 1=1
            and date(date_parse(np.timestamp_local, '%Y-%m-%d %H:%i:%s'))
                    >= date('{{start_date}}')
            and date(date_parse(np.timestamp_local, '%Y-%m-%d %H:%i:%s'))
                    <  date('{{end_date}}')
            -- Exclusive PU-or-DO airport tag.
            and ((pu_g.airport_code is not null and do_g.airport_code is null)
              or (do_g.airport_code is not null and pu_g.airport_code is null))
            -- City fallback: prefer rows where np.city = m.npi_city; otherwise
            -- accept m.city_order=1 only when no city-specific mapping exists.
            and (np.city = m.npi_city
                 or (m.city_order = 1
                     and not exists (
                        select 1 from mapping_v2 as m2
                        where m2.country = np.country
                            and m2.npi_city = np.city
                            and m2.uber_product = np.competitor
                            and m2.comp_product = np.competitor_app
                            and m2.product_number = 1
                            and m2.source = np.source)))),

    bucketed as (
        select
            r.airport_code                              as airport_code,
            r.trip_type                                 as trip_type,
            r.hex_id                                    as hex_id,
            r.competitor                                as competitor,
            case
                when r.city_id in ('1007', '531', '2010', '1911', '202', '599', '240', '1878', '2007', '2095', '1879', '214') then
                    case
                        when dow(r.ts_local) in (5, 6) then
                            case
                                when hour(r.ts_local) in (1, 2, 3, 4, 5, 6)        then 'wked_night'
                                when hour(r.ts_local) in (7, 8, 9, 10, 11)         then 'wked_morning'
                                when hour(r.ts_local) in (12, 13, 14, 15, 16, 17)  then 'wked_day'
                                when hour(r.ts_local) in (18, 19, 20)              then 'wrek_evening'
                                when hour(r.ts_local) in (21, 22, 23, 0, 24)       then 'wrek_late_evening'
                            else 'error' end
                        else
                            case
                                when hour(r.ts_local) in (1, 2, 3, 4, 5, 6)        then 'wkd_night'
                                when hour(r.ts_local) in (7, 8, 9, 10, 11)         then 'wkd_morning'
                                when hour(r.ts_local) in (12, 13, 14, 15, 16, 17)  then 'wkd_day'
                                when hour(r.ts_local) in (18, 19, 20)              then 'wkd_evening'
                                when hour(r.ts_local) in (21, 22, 23, 0, 24)       then 'wkd_late_evening'
                            else 'error' end
                    end
                else
                    case
                        when dow(r.ts_local) in (6, 7) then
                            case
                                when hour(r.ts_local) in (1, 2, 3, 4, 5, 6)        then 'wked_night'
                                when hour(r.ts_local) in (7, 8, 9, 10, 11)         then 'wked_morning'
                                when hour(r.ts_local) in (12, 13, 14, 15, 16, 17)  then 'wked_day'
                                when hour(r.ts_local) in (18, 19, 20)              then 'wrek_evening'
                                when hour(r.ts_local) in (21, 22, 23, 0, 24)       then 'wrek_late_evening'
                            else 'error' end
                        else
                            case
                                when hour(r.ts_local) in (1, 2, 3, 4, 5, 6)        then 'wkd_night'
                                when hour(r.ts_local) in (7, 8, 9, 10, 11)         then 'wkd_morning'
                                when hour(r.ts_local) in (12, 13, 14, 15, 16, 17)  then 'wkd_day'
                                when hour(r.ts_local) in (18, 19, 20)              then 'wkd_evening'
                                when hour(r.ts_local) in (21, 22, 23, 0, 24)       then 'wkd_late_evening'
                            else 'error' end
                    end
            end                                         as time_bucket,
            case
                when r.dist_km * 1.0 >= 0.0   and r.dist_km * 1.0 <= 5.0    then '0-5 km'
                when r.dist_km * 1.0 > 5.0    and r.dist_km * 1.0 <= 10.0   then '5-10 km'
                when r.dist_km * 1.0 > 10.0   and r.dist_km * 1.0 <= 20.0   then '10-20 km'
                when r.dist_km * 1.0 > 20.0   and r.dist_km * 1.0 <= 30.0   then '20-30 km'
                when r.dist_km * 1.0 > 30.0   and r.dist_km * 1.0 <= 40.0   then '30-40 km'
                when r.dist_km * 1.0 > 40.0   and r.dist_km * 1.0 <= 50.0   then '40-50 km'
                when r.dist_km * 1.0 > 50.0   and r.dist_km * 1.0 <= 60.0   then '50-60 km'
                when r.dist_km * 1.0 > 60.0   and r.dist_km * 1.0 <= 70.0   then '60-70 km'
                when r.dist_km * 1.0 > 70.0   and r.dist_km * 1.0 <= 80.0   then '70-80 km'
                when r.dist_km * 1.0 > 80.0   and r.dist_km * 1.0 <= 90.0   then '80-90 km'
                when r.dist_km * 1.0 > 90.0   and r.dist_km * 1.0 <= 100.0  then '90-100 km'
                when r.dist_km * 1.0 > 100.0                                then '100+ km'
            else 'error' end                            as dist_bucket
        from obs_raw as r
        where r.hex_id is not null)

select
    b.airport_code                  as airport_code,
    b.trip_type                     as trip_type,
    b.hex_id                        as hex_id,
    b.time_bucket                   as time_bucket,
    b.dist_bucket                   as dist_bucket,
    b.competitor                    as competitor,
    count(*)                        as num_observations
from bucketed as b
where 1=1
    and b.time_bucket <> 'error'
    and b.dist_bucket <> 'error'
group by 1, 2, 3, 4, 5, 6
