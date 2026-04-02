--
-- PostgreSQL database dump
--

\restrict 5ICKwjO5Ai1YeA6UATH0wbMEDn9ePWl12HQ9oF1ESTTKVZicBmgdVE9JGmByJvv

-- Dumped from database version 14.20 (Homebrew)
-- Dumped by pg_dump version 14.20 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: justinhu
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO justinhu;

--
-- Name: books; Type: TABLE; Schema: public; Owner: justinhu
--

CREATE TABLE public.books (
    id integer NOT NULL,
    title character varying,
    author character varying,
    is_active boolean,
    price double precision,
    description text,
    created_at timestamp with time zone DEFAULT now(),
    update_at timestamp with time zone,
    owner_id integer
);


ALTER TABLE public.books OWNER TO justinhu;

--
-- Name: books_id_seq; Type: SEQUENCE; Schema: public; Owner: justinhu
--

CREATE SEQUENCE public.books_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.books_id_seq OWNER TO justinhu;

--
-- Name: books_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: justinhu
--

ALTER SEQUENCE public.books_id_seq OWNED BY public.books.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: justinhu
--

CREATE TABLE public.users (
    id integer NOT NULL,
    username character varying NOT NULL,
    hashed_password character varying NOT NULL,
    is_active boolean NOT NULL
);


ALTER TABLE public.users OWNER TO justinhu;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: justinhu
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.users_id_seq OWNER TO justinhu;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: justinhu
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: books id; Type: DEFAULT; Schema: public; Owner: justinhu
--

ALTER TABLE ONLY public.books ALTER COLUMN id SET DEFAULT nextval('public.books_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: justinhu
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: justinhu
--

COPY public.alembic_version (version_num) FROM stdin;
f43bd26e2ca8
\.


--
-- Data for Name: books; Type: TABLE DATA; Schema: public; Owner: justinhu
--

COPY public.books (id, title, author, is_active, price, description, created_at, update_at, owner_id) FROM stdin;
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: justinhu
--

COPY public.users (id, username, hashed_password, is_active) FROM stdin;
1	justin	$argon2id$v=19$m=65536,t=3,p=4$CH+bZTZaIiVQN+lornakag$ziy8kESIXUUWJfyRxNfx6Pxp2qQLOR6iDIJJDAetQTY	t
2	wegun	$argon2id$v=19$m=65536,t=3,p=4$rIEDduTlCO+0INjBywohxQ$gCTs/HvykcFs4jaJi9Dnin622VYoDID52NtNTyTLgBE	t
\.


--
-- Name: books_id_seq; Type: SEQUENCE SET; Schema: public; Owner: justinhu
--

SELECT pg_catalog.setval('public.books_id_seq', 8, true);


--
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: justinhu
--

SELECT pg_catalog.setval('public.users_id_seq', 2, true);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: justinhu
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: books books_pkey; Type: CONSTRAINT; Schema: public; Owner: justinhu
--

ALTER TABLE ONLY public.books
    ADD CONSTRAINT books_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: justinhu
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: ix_books_id; Type: INDEX; Schema: public; Owner: justinhu
--

CREATE INDEX ix_books_id ON public.books USING btree (id);


--
-- Name: ix_books_title; Type: INDEX; Schema: public; Owner: justinhu
--

CREATE INDEX ix_books_title ON public.books USING btree (title);


--
-- Name: ix_users_id; Type: INDEX; Schema: public; Owner: justinhu
--

CREATE INDEX ix_users_id ON public.users USING btree (id);


--
-- Name: ix_users_username; Type: INDEX; Schema: public; Owner: justinhu
--

CREATE UNIQUE INDEX ix_users_username ON public.users USING btree (username);


--
-- Name: books books_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: justinhu
--

ALTER TABLE ONLY public.books
    ADD CONSTRAINT books_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id);


--
-- PostgreSQL database dump complete
--

\unrestrict 5ICKwjO5Ai1YeA6UATH0wbMEDn9ePWl12HQ9oF1ESTTKVZicBmgdVE9JGmByJvv

