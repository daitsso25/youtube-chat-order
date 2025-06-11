import streamlit as st
import pandas as pd
import re
from collections import defaultdict
import io
import logging
import sys
import json
import os

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

def save_buyer_ids(buyer_ids):
    """구매자 아이디를 세션에 저장"""
    st.session_state['buyer_ids'] = list(buyer_ids)
    # 캐시된 데이터도 업데이트
    if 'cached_buyer_ids' in st.session_state:
        st.session_state['cached_buyer_ids'] = list(buyer_ids)

@st.cache_data(persist="disk")
def load_cached_buyer_ids():
    """캐시된 구매자 아이디 불러오기"""
    return []

def load_buyer_ids():
    """저장된 구매자 아이디 불러오기"""
    try:
        # 세션 상태에서 먼저 확인
        if 'buyer_ids' in st.session_state:
            return set(st.session_state['buyer_ids'])
        
        # 캐시된 데이터 확인
        if 'cached_buyer_ids' not in st.session_state:
            st.session_state['cached_buyer_ids'] = load_cached_buyer_ids()
        
        return set(st.session_state['cached_buyer_ids'])
    except Exception as e:
        logging.error(f"구매자 아이디 불러오기 중 오류: {str(e)}")
        return set()

def clean_product_name(name):
    """상품명에서 불필요한 문구 제거"""
    # 제외할 단어 패턴 (주문 수량, 인원 제한 등)
    exclude_patterns = [
        r'선착순',
        r'한정',
        r'당일배송',
        r'익일배송',
        r'품절임박',
        r'마감임박',
        r'재고한정',
        r'한분만',
        r'한분',
        r'두분만',
        r'두분',
        r'세분만',
        r'세분',
        r'네분만',
        r'네분',
        r'다섯분만',
        r'다섯분',
        r'여섯분만',
        r'여섯분',
        r'일곱분만',
        r'일곱분',
        r'여덟분만',
        r'여덟분',
        r'아홉분만',
        r'아홉분',
        r'열분만',
        r'열분',
        r'출발'
    ]
    
    cleaned_name = name
    
    # '/' 이전의 텍스트만 처리
    if '/' in cleaned_name:
        cleaned_name = cleaned_name.split('/')[0].strip()
    
    # 제외 패턴 적용
    for pattern in exclude_patterns:
        cleaned_name = re.sub(pattern, '', cleaned_name, flags=re.IGNORECASE)
    
    # 구매자명 패턴 제거 (한글/영문 + 숫자)
    cleaned_name = re.sub(r'[가-힣a-zA-Z]+\d+(?=\s|$)', '', cleaned_name)
    
    # 연속된 공백 제거 및 앞뒤 공백 제거
    cleaned_name = ' '.join(cleaned_name.split())
    return cleaned_name.strip()

def extract_product_info(msg):
    """상품 정보(상품 번호, 상품명, 가격) 추출"""
    try:
        # 상품 번호와 내용 분리
        number_pattern = r'^(\d+)[\s.]*(.+)'
        number_match = re.match(number_pattern, msg.strip())
        
        if not number_match:
            return None, None, None
            
        product_number = int(number_match.group(1))
        remaining_text = number_match.group(2).strip()
        
        # 가격 패턴: 숫자 + '원'
        price_pattern = r'(\d{3,6})원'
        price_match = re.search(price_pattern, remaining_text)
        
        if price_match:
            price = int(price_match.group(1))
            # 가격 이전의 텍스트를 상품명으로
            product_name = remaining_text[:price_match.start()].strip()
            
            # 상품명이 비어있지 않고 가격이 있는 경우
            if product_name and price:
                # 상품명 정제 (마지막 가격 부분 제거)
                product_name = re.sub(r'\s*\d+원\s*$', '', product_name)
                # 상품명 정제 (불필요한 문구 제거)
                product_name = clean_product_name(product_name)
                return product_number, product_name, price
                
    except Exception as e:
        logging.error(f"상품 정보 추출 중 오류: {str(e)}, 메시지: {msg}")
    return None, None, None

def get_valid_buyers(df, manual_buyer_ids=None):
    """유효한 구매자 목록 생성"""
    buyers = set()
    
    # 수동으로 입력된 구매자 아이디 추가
    if manual_buyer_ids:
        buyers.update(manual_buyer_ids)
    
    for _, row in df.iterrows():
        msg = str(row["메시지"]).strip() if pd.notna(row["메시지"]) else ""
        if '/' in msg:
            # '/' 뒤의 텍스트에서 구매자 찾기
            order_part = msg.split('/')[-1].strip()
            parts = order_part.split()
            
            for part in parts:
                found_buyer = None
                
                # 먼저 수동으로 입력된 구매자 ID와 일치하는지 확인
                if manual_buyer_ids:
                    for buyer_id in manual_buyer_ids:
                        if part.startswith(buyer_id):
                            found_buyer = buyer_id
                            break
                
                # 수동 입력된 구매자를 찾지 못한 경우, 일반적인 패턴 확인
                if not found_buyer:
                    # 구매자 이름 패턴 (한글/영문)
                    match = re.match(r'^([가-힣a-zA-Z!~]+)', part)
                    if match:
                        buyer = match.group(1).strip()
                        if buyer:
                            buyers.add(buyer)
    
    return buyers

def parse_order_info(msg, valid_buyers):
    """주문 정보(구매자와 수량) 추출"""
    try:
        if '/' in msg:
            order_part = msg.split('/')[-1].strip()
            orders = []
            
            # 현재 처리 중인 문자열
            remaining_text = order_part
            
            while remaining_text:
                remaining_text = remaining_text.strip()
                if not remaining_text:
                    break
                    
                found_buyer = None
                quantity = 1
                match_length = 0
                
                # 1. 먼저 수동으로 입력된 구매자 ID 확인
                if valid_buyers:
                    for buyer_id in valid_buyers:
                        if remaining_text.startswith(buyer_id):
                            found_buyer = buyer_id
                            match_length = len(buyer_id)
                            # 구매자 ID 뒤의 숫자를 수량으로 처리
                            quantity_match = re.match(r'\d+', remaining_text[match_length:])
                            if quantity_match:
                                quantity = int(quantity_match.group())
                                match_length += len(quantity_match.group())
                            break
                
                # 2. 수동 입력된 구매자를 찾지 못한 경우, 일반적인 패턴 확인
                if not found_buyer:
                    # 한글/영문 이름 + 숫자(수량) 패턴
                    match = re.match(r'([가-힣a-zA-Z!~]+)(\d*)', remaining_text)
                    if match:
                        potential_buyer = match.group(1)
                        if potential_buyer in valid_buyers:
                            found_buyer = potential_buyer
                            match_length = len(match.group(1))
                            if match.group(2):  # 수량이 있는 경우
                                quantity = int(match.group(2))
                                match_length += len(match.group(2))
                
                if found_buyer:
                    orders.append((found_buyer, quantity))
                    remaining_text = remaining_text[match_length:]
                else:
                    # 매칭되지 않은 경우, 다음 문자로 이동
                    remaining_text = remaining_text[1:]
            
            return orders
            
        return []
    except Exception as e:
        logging.error(f"주문 정보 파싱 중 오류: {str(e)}, 메시지: {msg}")
        return []

def process_excel(df, manual_buyer_ids):
    try:
        # 필수 컬럼 확인
        required_columns = ["메시지", "사용자"]
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            st.error(f"필수 컬럼이 없습니다: {', '.join(missing_columns)}")
            return None

        # 관리자 메시지만 필터링
        admins = ["만물다잇쏘", "다잇쏘"]
        df = df[df["사용자"].isin(admins)]

        if df.empty:
            st.error("관리자(만물다잇쏘, 다잇쏘)의 메시지를 찾을 수 없습니다.")
            return None

        # 메시지를 시간순으로 정렬
        if "시간" in df.columns:
            df = df.sort_values("시간")
        
        # 유효한 구매자 목록 생성
        valid_buyers = get_valid_buyers(df, manual_buyer_ids)
        logging.info(f"유효한 구매자 목록: {valid_buyers}")
        
        # 주문 정보 처리
        all_orders = []

        for _, row in df.iterrows():
            msg = str(row["메시지"]).strip() if pd.notna(row["메시지"]) else ""
            if not msg:
                continue

            logging.info(f"처리 중인 메시지: {msg}")

            # 상품 정보와 주문 정보 추출
            product_number, product_name, price = extract_product_info(msg)
            
            if product_number and product_name and price and '/' in msg:
                orders = parse_order_info(msg, valid_buyers)
                
                for buyer, quantity in orders:
                    order = {
                        "구매자": buyer,
                        "상품번호": product_number,
                        "상품명": product_name,
                        "판매가": price,
                        "수량": quantity
                    }
                    all_orders.append(order)
                    logging.info(f"주문 처리: {buyer} - {product_name} {quantity}개")

        if not all_orders:
            st.warning("처리된 주문 내역이 없습니다.")
            if st.checkbox("처리된 메시지 보기"):
                st.write("### 처리된 메시지 목록")
                messages = df["메시지"].dropna().tolist()
                for msg in messages:
                    st.text(msg)
            return None

        # 데이터프레임 생성 및 정렬
        df_orders = pd.DataFrame(all_orders)
        df_orders = df_orders.sort_values(by=['구매자', '상품번호']).reset_index(drop=True)
        
        return df_orders

    except Exception as e:
        logging.error(f"데이터 처리 중 오류가 발생했습니다: {str(e)}")
        st.error(f"데이터 처리 중 오류가 발생했습니다: {str(e)}")
        return None

def main():
    try:
        st.set_page_config(
            page_title="유튜브 채팅 주문 정리기",
            layout="centered",
            initial_sidebar_state="collapsed"
        )
        
        # CSS 스타일 추가
        st.markdown("""
            <style>
                .stApp {
                    max-width: 1200px;
                    margin: 0 auto;
                }
                .stButton>button {
                    width: 100%;
                }
                .streamlit-expanderHeader {
                    background-color: #f0f2f6;
                }
            </style>
        """, unsafe_allow_html=True)
        
        st.title("🧾 유튜브 채팅 주문 정리기")
        
        # 사용 방법 섹션
        with st.expander("📖 사용 방법"):
            st.markdown("""
                1. 숫자가 포함된 구매자 아이디가 있다면 입력해주세요.
                2. 유튜브 채팅 내보내기 엑셀 파일을 업로드해주세요.
                3. 자동으로 주문이 정리되어 표시됩니다.
                4. 정리된 주문 내역을 엑셀 파일로 다운로드할 수 있습니다.
            """)

        # 저장된 구매자 아이디 불러오기
        saved_buyer_ids = load_buyer_ids()
        
        # 숫자가 포함된 구매자 아이디 입력 섹션
        st.subheader("숫자가 포함된 구매자 아이디 설정")
        st.info("구매자 아이디에 숫자가 포함된 경우, 아래에 입력해주세요. (예: user123, buyer777)")
        
        # 저장된 구매자 아이디가 있으면 표시
        default_buyer_ids = ", ".join(saved_buyer_ids) if saved_buyer_ids else ""
        buyer_ids_input = st.text_area(
            "구매자 아이디 입력 (쉼표로 구분)", 
            value=default_buyer_ids,
            help="숫자가 포함된 구매자 아이디를 쉼표(,)로 구분하여 입력하세요.\n예: user123, buyer777"
        )
        
        # 입력된 구매자 아이디 처리
        manual_buyer_ids = set()
        if buyer_ids_input:
            manual_buyer_ids = {id.strip() for id in buyer_ids_input.split(',') if id.strip()}
            
            # 변경사항이 있으면 저장
            if manual_buyer_ids != saved_buyer_ids:
                save_buyer_ids(manual_buyer_ids)
                if len(manual_buyer_ids) > len(saved_buyer_ids):
                    st.success("✅ 새로운 구매자 아이디가 저장되었습니다.")
                elif len(manual_buyer_ids) < len(saved_buyer_ids):
                    st.warning("⚠️ 일부 구매자 아이디가 제거되었습니다.")
                else:
                    st.info("ℹ️ 구매자 아이디가 업데이트되었습니다.")

        # 현재 등록된 구매자 아이디 표시
        if manual_buyer_ids:
            with st.expander("등록된 구매자 아이디 목록"):
                st.write(sorted(list(manual_buyer_ids)))

        uploaded_file = st.file_uploader("채팅 엑셀 파일을 업로드하세요 (.xlsx)", type=["xlsx"])

        if uploaded_file:
            try:
                df = pd.read_excel(uploaded_file)
                logging.info(f"파일 업로드 완료: {uploaded_file.name}")
                
                # process_excel 함수 호출 시 manual_buyer_ids 전달
                df_orders = process_excel(df, manual_buyer_ids)
                
                if df_orders is not None and not df_orders.empty:
                    # 주문 상세 내역 표시
                    st.subheader("주문 상세 내역")
                    
                    # 스타일이 적용된 데이터프레임 표시
                    st.dataframe(
                        df_orders,
                        column_config={
                            "구매자": st.column_config.TextColumn("구매자", width=150),
                            "상품번호": st.column_config.NumberColumn("상품번호", format="%d번"),
                            "상품명": st.column_config.TextColumn("상품명", width=200),
                            "판매가": st.column_config.NumberColumn("판매가", format="%d원"),
                            "수량": st.column_config.NumberColumn("수량", format="%d개"),
                        },
                        hide_index=True,
                    )
                    
                    # 구매자별 합계 계산
                    df_summary = df_orders.copy()
                    df_summary['구매금액'] = df_summary['판매가'] * df_summary['수량']
                    df_summary = df_summary.groupby("구매자").agg({
                        "구매금액": "sum",
                        "수량": "sum"
                    }).rename(columns={
                        "구매금액": "총구매금액",
                        "수량": "총주문수량"
                    }).reset_index()
                    
                    # 구매자별 합계 표시
                    st.subheader("구매자별 합계")
                    st.dataframe(
                        df_summary,
                        column_config={
                            "구매자": st.column_config.TextColumn("구매자", width=150),
                            "총주문수량": st.column_config.NumberColumn("총주문수량", format="%d개"),
                            "총구매금액": st.column_config.NumberColumn("총구매금액", format="%d원"),
                        },
                        hide_index=True,
                    )

                    # 엑셀 저장
                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
                        df_orders.to_excel(writer, sheet_name="주문상세", index=False)
                        df_summary.to_excel(writer, sheet_name="구매자별합계", index=False)
                    output.seek(0)

                    st.success("주문 내역이 정리되었습니다 ✅")
                    st.download_button("📥 정리된 주문 엑셀 다운로드", output, file_name="정리된_주문내역.xlsx")
                
            except Exception as e:
                logging.error(f"파일 처리 중 오류가 발생했습니다: {str(e)}")
                st.error(f"파일 처리 중 오류가 발생했습니다: {str(e)}")
                st.error("파일 형식과 내용을 확인해주세요.")
    except Exception as e:
        logging.error(f"프로그램 실행 중 오류가 발생했습니다: {str(e)}")
        st.error("프로그램 실행 중 오류가 발생했습니다. 관리자에게 문의해주세요.")

if __name__ == "__main__":
    main()
